"""Official A2A transport around the existing AX execution and role policy."""

import asyncio
import socket
import threading
import time

import uvicorn
from google.protobuf.json_format import MessageToDict, ParseDict
from starlette.applications import Starlette
from starlette.responses import JSONResponse

from a2a.server.request_handlers.request_handler import RequestHandler, validate_request_params
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.types import a2a_pb2 as types
from a2a.utils import errors

from popot_agents.runtime_config import RUNTIME
from .delegation import ACTIVE, A2AError


class AXRequestHandler(RequestHandler):
    def __init__(self, service, role):
        self.service, self.role = service, role

    async def _dispatch(self, method, params, context):
        authorization = context.state["headers"].get("authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        try:
            return await asyncio.to_thread(self.service.dispatch, token, self.role, method,
                                           MessageToDict(params))
        except A2AError as exc:
            error = {-32001: errors.TaskNotFoundError,
                     -32002: errors.TaskNotCancelableError}.get(exc.code, errors.InvalidParamsError)
            raise error(str(exc)) from None
        except Exception:
            # The SDK serializes error messages: do not forward storage exceptions.
            raise errors.InternalError("delegation storage unavailable") from None

    @validate_request_params
    async def on_message_send(self, params, context):
        config = params.configuration
        if config.HasField("task_push_notification_config"):
            raise errors.PushNotificationNotSupportedError()
        if config.accepted_output_modes and "text/plain" not in config.accepted_output_modes:
            raise errors.ContentTypeNotSupportedError()
        task = (await self._dispatch("SendMessage", params, context))["task"]
        # A2A 1.0 uses returnImmediately, unlike the earlier blocking field.
        if not config.return_immediately:
            while task["status"]["state"] in ACTIVE:
                await asyncio.sleep(self.service.policy["poll_interval_seconds"])
                task = await self._dispatch("GetTask", types.GetTaskRequest(id=task["id"]), context)
        return ParseDict(task, types.Task())

    @validate_request_params
    async def on_get_task(self, params, context):
        return ParseDict(await self._dispatch("GetTask", params, context), types.Task())

    @validate_request_params
    async def on_cancel_task(self, params, context):
        return ParseDict(await self._dispatch("CancelTask", params, context), types.Task())

    async def on_list_tasks(self, params, context):
        raise errors.UnsupportedOperationError()

    async def on_message_send_stream(self, params, context):
        raise errors.UnsupportedOperationError("Streaming is not enabled")
        yield

    async def on_subscribe_to_task(self, params, context):
        raise errors.UnsupportedOperationError("Streaming is not enabled")
        yield

    async def on_create_task_push_notification_config(self, params, context):
        raise errors.PushNotificationNotSupportedError()

    async def on_get_task_push_notification_config(self, params, context):
        raise errors.PushNotificationNotSupportedError()

    async def on_list_task_push_notification_configs(self, params, context):
        raise errors.PushNotificationNotSupportedError()

    async def on_delete_task_push_notification_config(self, params, context):
        raise errors.PushNotificationNotSupportedError()

    async def on_get_extended_agent_card(self, params, context):
        raise errors.ExtendedAgentCardNotConfiguredError()


class DelegationBoundary:
    """Keep actor authorization and bounded request reads outside the protocol."""

    def __init__(self, app, service):
        self.app, self.service = app, service

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        role = scope["path"].removeprefix("/a2a/")
        if scope["path"] != f"/a2a/{role}" or role not in self.service.roles:
            return await JSONResponse({}, status_code=404)(scope, receive, send)
        headers = dict(scope["headers"])
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        try:
            self.service._authorized(token, role)
        except A2AError:
            return await JSONResponse({}, status_code=403)(scope, receive, send)

        async def read_body():
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return None
                body.extend(message.get("body", b""))
                if len(body) > RUNTIME["limits"]["request_bytes"]:
                    return False
                if not message.get("more_body", False):
                    return bytes(body)

        try:
            body = await asyncio.wait_for(read_body(), timeout=5)
        except asyncio.TimeoutError:
            return await JSONResponse({}, status_code=408)(scope, receive, send)
        if body is None:
            return
        if body is False:
            return await JSONResponse({}, status_code=413)(scope, receive, send)
        delivered = False

        async def replay():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        return await self.app(scope, replay, send)


def create_a2a_app(service):
    routes = []
    for role in service.roles:
        prefix = f"/a2a/{role}"
        card = ParseDict(service.card(role), types.AgentCard())
        routes.extend(create_agent_card_routes(card, card_url=prefix + "/.well-known/agent-card.json"))
        routes.extend(create_jsonrpc_routes(AXRequestHandler(service, role), prefix))
    return DelegationBoundary(Starlette(routes=routes), service)


class A2AServer:
    """Run the SDK ASGI routes alongside the synchronous local API."""

    def __init__(self, service, host, port):
        self.socket = socket.socket()
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((host, port))
            self.server_address = self.socket.getsockname()
            self.server_port = self.server_address[1]
            if port == 0:
                service.base_url = f"http://{host}:{self.server_port}"
            app = create_a2a_app(service)
            self.server = uvicorn.Server(uvicorn.Config(
                app, log_config=None, access_log=False, log_level="warning", lifespan="off",
                loop="asyncio", http="h11", ws="none", limit_concurrency=64,
                timeout_keep_alive=5, timeout_graceful_shutdown=5))
            self.thread = threading.Thread(target=self.server.run,
                                           kwargs={"sockets": [self.socket]}, daemon=True,
                                           name="popot-a2a")
            self.thread.start()
            deadline = time.monotonic() + 10
            while not self.server.started:
                if not self.thread.is_alive() or time.monotonic() >= deadline:
                    self.shutdown()
                    raise RuntimeError("A2A server did not start")
                time.sleep(0.01)
        except BaseException:
            self.socket.close()
            raise

    def shutdown(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise RuntimeError("A2A server did not stop")

    def server_close(self):
        self.shutdown()
        self.socket.close()

"""MCP tools that call the existing orchestrator HTTP API."""

import json
import asyncio
from collections import deque
from dataclasses import replace
import os
import sys
import uuid
from urllib import error, parse, request

from popot_agents.runtime_config import RUNTIME
from popot_agents.progress import is_progress_message

MAX_LOG_BODY_BYTES = RUNTIME["logging"]["mcp_body_max_bytes"]
SECRET_NAMES = {"authorization", "proxy-authorization", "cookie", "set-cookie",
                "mcp-session-id", "x-api-key", "api_key", "apikey", "password",
                "secret", "token", "access_token", "refresh_token"}


def _secret_name(name: str) -> bool:
    name = name.lower()
    return name in SECRET_NAMES or name.endswith(
        ("-api-key", "_api_key", "-password", "_password", "-secret",
         "_secret", "-token", "_token"))


def _safe_json(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if _secret_name(key) else _safe_json(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_json(item) for item in value]
    return value


def _safe_headers(headers):
    return [[name.decode("latin-1"),
             "[REDACTED]" if _secret_name(name.decode("latin-1"))
             else value.decode("latin-1")]
            for name, value in headers]


def _log_mcp_event(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True)


def _normalized_session_id(session_id: str | None) -> str | None:
    if session_id is None:
        return None
    return session_id.strip() or None


def _require_target(role: str | None, agent: str | None,
                    session_id: str | None) -> None:
    for name, value in (("role", role), ("agent", agent)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{name} must be a nonempty string")
    if session_id is None and role is None and agent is None:
        raise ValueError("To start a chat, provide role or agent; "
                         "to continue, provide session_id")


def _describe_chat_tool_schema(schema: dict, names: tuple[str, ...]) -> None:
    """Advertise the alternatives MCPServer cannot infer from optional arguments."""
    schema["anyOf"] = [
        {"required": [name],
         "properties": {name: {"type": "string", "minLength": 1,
                                "pattern": r"\S"}}}
        for name in names
    ]
    properties = schema.setdefault("properties", {})
    descriptions = {
        "role": "Configured role for a new session. Use 'chat' for general questions. "
                "Ask the user if the intended role is unclear.",
        "agent": "Configured agent profile for a new session; use this instead of role "
                 "only when the user specifies an agent profile.",
        "sessionId": "Existing session ID returned by run_task; omit when starting a new session.",
        "session_id": "Existing session ID returned by a previous response; "
                      "omit when starting a new session.",
    }
    for name in names:
        properties.setdefault(name, {})["description"] = descriptions[name]
        if name in {"role", "agent"}:
            properties[name]["pattern"] = r"\S"


class MCPHTTPLoggingMiddleware:
    """Log HTTP metadata and bounded JSON requests without buffering responses."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        request_id = uuid.uuid4().hex[:12]
        client = scope.get("client") or (None, None)
        headers = scope.get("headers", [])
        _log_mcp_event({
            "event": "mcp_request", "request_id": request_id,
            "client_ip": client[0], "client_port": client[1],
            "method": scope.get("method"), "path": scope.get("path"),
            "headers": _safe_headers(headers),
        })
        captured = bytearray()
        body_size = 0
        body_logged = False

        async def logged_receive():
            nonlocal body_size, body_logged
            message = await receive()
            if message["type"] == "http.request" and not body_logged:
                chunk = message.get("body", b"")
                body_size += len(chunk)
                if len(captured) < MAX_LOG_BODY_BYTES:
                    captured.extend(chunk[:MAX_LOG_BODY_BYTES - len(captured)])
                if not message.get("more_body", False):
                    content_type = next((value.decode("latin-1") for name, value in headers
                                         if name.lower() == b"content-type"), "")
                    event = {"event": "mcp_request_body", "request_id": request_id,
                             "body_bytes": body_size}
                    if body_size > MAX_LOG_BODY_BYTES:
                        event["body_truncated"] = True
                    elif content_type.lower().startswith("application/json"):
                        try:
                            event["body"] = _safe_json(json.loads(captured))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            event["invalid_json"] = True
                    _log_mcp_event(event)
                    body_logged = True
            return message

        async def logged_send(message):
            await send(message)
            if message["type"] == "http.response.start":
                _log_mcp_event({
                    "event": "mcp_response", "request_id": request_id,
                    "status": message["status"],
                    "headers": _safe_headers(message.get("headers", [])),
                })

        try:
            return await self.app(scope, logged_receive, logged_send)
        except Exception as exc:
            _log_mcp_event({"event": "mcp_error", "request_id": request_id,
                            "error_type": type(exc).__name__})
            raise


class OrchestratorClient:
    def __init__(self, base_url: str,
                 timeout_seconds: int = RUNTIME["timeouts"]["mcp_upstream_seconds"]) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        headers = {"User-Agent": "popot-agents-mcp/1"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        call = request.Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with request.urlopen(call, timeout=self.timeout_seconds) as response:
                result = json.load(response)
        except error.HTTPError as exc:
            try:
                detail = json.load(exc).get("error", "request failed")
            except (ValueError, AttributeError):
                detail = "request failed"
            raise RuntimeError(f"orchestrator HTTP {exc.code}: {detail}") from exc
        except (error.URLError, TimeoutError) as exc:
            raise RuntimeError("orchestrator is unavailable or timed out") from exc
        except ValueError as exc:
            raise RuntimeError("orchestrator returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise RuntimeError("orchestrator returned an invalid response")
        return result

    def run_task(self, task: str, role: str | None = None,
                 agent: str | None = None, session_id: str | None = None) -> dict:
        session_id = _normalized_session_id(session_id)
        _require_target(role, agent, session_id)
        if session_id is not None:
            return self.send_message(task, session_id=session_id, role=role, agent=agent)
        payload = {"task": task}
        if role is not None:
            payload["role"] = role
        if agent is not None:
            payload["agent"] = agent
        return self._request("POST", "/tasks", payload)

    def send_message(self, message: str, session_id: str | None = None,
                     role: str | None = None, agent: str | None = None) -> dict:
        session_id = _normalized_session_id(session_id)
        _require_target(role, agent, session_id)
        payload = {"message": message}
        if session_id is not None:
            payload["sessionId"] = session_id
        if role is not None:
            payload["role"] = role
        if agent is not None:
            payload["agent"] = agent
        return self._request("POST", "/messages", payload)

    def list_chats(self) -> dict:
        return self._request("GET", "/chats")

    def get_chat(self, session_id: str) -> dict:
        return self._request("GET", "/chats/" + parse.quote(session_id, safe=""))


async def stream_call(client, path, payload, context):
    """Stream short actions using HTTPX2, already supplied by the MCP SDK.

    Cancelling the MCP call closes its HTTP stream; agent work follows the
    orchestrator's existing disconnect policy and is never automatically retried.
    """
    import httpx2
    meta = context.request_context.meta
    token = meta.get('progress_token') if meta is not None else None
    progress_enabled = token is not None and token != ''
    activity = deque(maxlen=20)
    counter = 0
    try:
        async with httpx2.AsyncClient(timeout=client.timeout_seconds, trust_env=False) as http:
            async with http.stream('POST', client.base_url + path, json=payload,
                                   headers={'Accept': 'application/x-ndjson',
                                            'User-Agent': 'popot-agents-mcp/1'}) as response:
                if not response.headers.get('content-type', '').startswith('application/x-ndjson'):
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 8 * 1024 * 1024:
                            raise ValueError('response too large')
                    result = json.loads(body)
                    if not isinstance(result, dict):
                        raise ValueError('invalid response')
                    if response.status_code >= 400:
                        raise RuntimeError(f'orchestrator HTTP {response.status_code}: {result.get("error", "request failed")}')
                    return result
                pending = b''
                async for chunk in response.aiter_bytes():
                    pending += chunk
                    if len(pending) > 8 * 1024 * 1024:
                        raise ValueError('response too large')
                    while b'\n' in pending:
                        line, pending = pending.split(b'\n', 1)
                        item = json.loads(line)
                        if not isinstance(item, dict):
                            raise ValueError('invalid response')
                        if item.get('type') == 'progress':
                            message = item.get('message')
                            if is_progress_message(message):
                                activity.append(message)
                                counter += 1
                                if progress_enabled:
                                    try:
                                        await asyncio.wait_for(context.report_progress(counter, message=message), timeout=2)
                                    except Exception:
                                        pass  # Reporting must not replace or repeat the task result.
                        elif item.get('type') == 'result':
                            result = item.get('result')
                            status = item.get('status')
                            if not isinstance(result, dict) or not isinstance(status, int):
                                raise ValueError('invalid result')
                            if status >= 400:
                                raise RuntimeError(f'orchestrator HTTP {status}: {result.get("error", "request failed")}')
                            return {**result, 'activity': list(activity)}
                raise ValueError('stream ended without a result')
    except (httpx2.HTTPError, TimeoutError) as exc:
        raise RuntimeError('orchestrator is unavailable or timed out') from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError('orchestrator returned invalid progress stream') from exc


async def _omit_null_progress_token(ctx, call_next):
    """Preserve the gateway's null = no progress contract before SDK validation."""
    if ctx.method == "tools/call" and ctx.params is not None:
        meta = ctx.params.get("_meta")
        if isinstance(meta, dict) and "progressToken" in meta and meta["progressToken"] is None:
            meta = {key: value for key, value in meta.items() if key != "progressToken"}
            ctx = replace(ctx, params={**ctx.params, "_meta": meta})
    return await call_next(ctx)


def main() -> None:
    from mcp.server.mcpserver import MCPServer, Context
    from mcp.server.transport_security import TransportSecuritySettings

    class LoggedMCPServer(MCPServer):
        def streamable_http_app(self, **settings):
            return MCPHTTPLoggingMiddleware(super().streamable_http_app(**settings))

        async def list_tools(self):
            tools = await super().list_tools()
            for tool in tools:
                if tool.name == "run_task":
                    _describe_chat_tool_schema(tool.input_schema,
                                               ("role", "agent", "sessionId", "session_id"))
                elif tool.name == "send_message":
                    _describe_chat_tool_schema(tool.input_schema,
                                               ("session_id", "role", "agent"))
            return tools

    client = OrchestratorClient(
        os.getenv("MCP_ORCHESTRATOR_URL", "http://127.0.0.1:8002"),
        int(os.getenv("MCP_UPSTREAM_TIMEOUT_SECONDS", str(RUNTIME["timeouts"]["mcp_upstream_seconds"]))),
    )
    server = LoggedMCPServer("popot-agents", middleware=[_omit_null_progress_token])

    @server.tool()
    async def run_task(task: str, role: str | None = None, agent: str | None = None,
                       sessionId: str | None = None, session_id: str | None = None,
                       ctx: Context = None) -> dict:
        """Start a task with role (use chat for general questions) or agent. For a follow-up,
        pass the returned sessionId or session_id instead. If the role is unclear, ask the user.
        """
        sessionId = _normalized_session_id(sessionId)
        session_id = _normalized_session_id(session_id)
        if sessionId is not None and session_id is not None and sessionId != session_id:
            raise ValueError("sessionId and session_id must match")
        selected = sessionId if sessionId is not None else session_id
        if ctx is None:
            return await asyncio.to_thread(client.run_task, task, role=role, agent=agent, session_id=selected)
        _require_target(role, agent, selected)
        payload = {'message' if selected else 'task': task}
        if selected:
            payload['sessionId'] = selected
        if role is not None:
            payload['role'] = role
        if agent is not None:
            payload['agent'] = agent
        return await asyncio.wait_for(stream_call(client, '/messages' if selected else '/tasks', payload, ctx),
                                      timeout=client.timeout_seconds)

    @server.tool()
    async def send_message(message: str, session_id: str | None = None,
                           role: str | None = None, agent: str | None = None,
                           ctx: Context = None) -> dict:
        """Continue a chat with its returned session_id. For a new chat, use run_task;
        if starting here, provide role or agent. Ask the user when the role is unclear.
        """
        selected = _normalized_session_id(session_id)
        if ctx is None:
            return await asyncio.to_thread(client.send_message, message, session_id=selected, role=role, agent=agent)
        _require_target(role, agent, selected)
        payload = {'message': message}
        if selected:
            payload['sessionId'] = selected
        if role is not None:
            payload['role'] = role
        if agent is not None:
            payload['agent'] = agent
        return await asyncio.wait_for(stream_call(client, '/messages', payload, ctx), timeout=client.timeout_seconds)

    @server.tool()
    def list_chats() -> dict:
        """List saved chats and their session IDs, roles, status, and expiry."""
        return client.list_chats()

    @server.tool()
    def get_chat(session_id: str) -> dict:
        """Get the status and metadata of one saved chat."""
        return client.get_chat(session_id)

    server.run(
        transport="streamable-http",
        host=os.getenv("MCP_HOST", "127.0.0.1"),
        port=int(os.getenv("MCP_PORT", "8001")),
        stateless_http=True,
        json_response=False,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*"],
        ),
    )


if __name__ == "__main__":
    main()

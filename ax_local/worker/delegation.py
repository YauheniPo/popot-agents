"""Expose allowed AX roles as a bounded A2A delegation tool to the model."""

import asyncio
import hashlib
import json
import os
import time
from urllib.parse import urlsplit

import httpx
from google.protobuf.json_format import MessageToDict, ParseError
from a2a.client.client import ClientCallContext
from a2a.client.errors import A2AClientError
from a2a.client.transports.jsonrpc import JsonRpcTransport
from a2a.types import a2a_pb2 as types
from a2a.utils.errors import A2AError as SDKError
from a2a.utils.proto_utils import validate_proto_required_fields

from popot_agents.runtime_config import RUNTIME
from popot_agents.progress import forward_progress, is_progress_message


def make_tools(environ: dict | None = None) -> dict:
    environ = os.environ if environ is None else environ
    url, token = environ.get("AX_DELEGATION_URL"), environ.get("AX_DELEGATION_TOKEN")
    allowed = json.loads(environ.get("AX_DELEGATION_ROLES", "[]"))
    if not url or not token or not allowed:
        return {}
    descriptions = json.loads(environ.get("AX_DELEGATION_DESCRIPTIONS", "{}"))
    catalog = "\n".join(f"- {role}: {descriptions.get(role, 'Specialist role: ' + role)}"
                        for role in allowed)
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname \
            or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("invalid AX delegation endpoint")
    schema = {"type": "function", "function": {
        "name": "delegate_task",
        "description": "Start a separate AX agent with an allowed role and wait for its result. "
                       "Give it the task and all required context explicitly. Its workspace and "
                       "conversation are separate. Select by the role descriptions below. "
                       "For code review, send requirements, the diff and relevant source, test "
                       "results, or a readable repository/ref/PR; a local path alone is insufficient. "
                       "Use its result in your final answer. Available specialists:\n" + catalog,
        "parameters": {"type": "object", "properties": {
            "role": {"type": "string", "enum": allowed},
            "task": {"type": "string", "description": "Task with requirements and relevant input data"}},
            "required": ["role", "task"], "additionalProperties": False}}}
    poll_interval = float(environ.get("AX_DELEGATION_POLL_SECONDS", "0.5"))

    def delegate(arguments, *, timeout_seconds: float, call_id: str) -> str:
        if not isinstance(arguments, dict) or set(arguments) != {"role", "task"} \
                or arguments["role"] not in allowed or not isinstance(arguments["task"], str) \
                or not arguments["task"].strip() \
                or len(arguments["task"]) > RUNTIME["limits"]["message_chars"]:
            return json.dumps({"isError": True,
                               "error": "delegate_task requires an allowed role and a nonempty task"})
        deadline = time.monotonic() + timeout_seconds
        endpoint = url.rstrip("/") + "/a2a/" + arguments["role"]
        task_id = None
        progress_seq = 0

        def forward_task_progress(task):
            nonlocal progress_seq
            activity = MessageToDict(task).get('metadata', {}).get('activity', [])
            if not isinstance(activity, list):
                return
            for item in activity[:64]:
                if not isinstance(item, dict):
                    continue
                seq, message = item.get('seq'), item.get('message')
                if isinstance(seq, (int, float)) and progress_seq < seq <= 2**53 \
                        and seq == int(seq) and is_progress_message(message):
                    progress_seq = int(seq)
                    forward_progress(message)

        async def execute():
            nonlocal task_id
            headers = {"A2A-Version": "1.0", "Authorization": "Bearer " + token,
                       "Accept-Encoding": "identity"}
            async with httpx.AsyncClient(headers=headers, trust_env=False,
                                         transport=BoundedHTTPTransport()) as client:
                transport = JsonRpcTransport(client, types.AgentCard(), endpoint)

                async def invoke(operation, *, cleanup=False):
                    remaining = 2 if cleanup else deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("delegation deadline exceeded")
                    context = ClientCallContext(timeout=min(5, remaining))
                    response = await asyncio.wait_for(operation(context), timeout=min(5, remaining))
                    validate_proto_required_fields(response)
                    return response

                params = types.SendMessageRequest(
                    message=types.Message(message_id=hashlib.sha256(call_id.encode()).hexdigest(),
                                          role=types.ROLE_USER,
                                          parts=[types.Part(text=arguments["task"])]),
                    configuration=types.SendMessageConfiguration(return_immediately=True))
                try:
                    try:
                        response = await invoke(lambda ctx: transport.send_message(params, context=ctx))
                    except (A2AClientError, asyncio.TimeoutError) as exc:
                        # Only retry transport failures; the message ID deduplicates side effects.
                        if not isinstance(exc, asyncio.TimeoutError) and not isinstance(
                                exc.__cause__, httpx.RequestError):
                            raise
                        response = await invoke(lambda ctx: transport.send_message(params, context=ctx))
                    if not response.HasField("task"):
                        raise ValueError("delegation did not return a task")
                    task = response.task
                    task_id = task.id
                    forward_task_progress(task)
                    while task.status.state in {types.TASK_STATE_SUBMITTED, types.TASK_STATE_WORKING}:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("delegation deadline exceeded")
                        await asyncio.sleep(min(poll_interval, remaining))
                        task = await invoke(lambda ctx: transport.get_task(
                            types.GetTaskRequest(id=task_id), context=ctx))
                        forward_task_progress(task)
                    result = {"sessionId": task_id, "role": arguments["role"],
                              "state": types.TaskState.Name(task.status.state)}
                    if task.status.state == types.TASK_STATE_COMPLETED:
                        answer = "\n".join(part.text for artifact in task.artifacts
                                           for part in artifact.parts if part.HasField("text"))
                        limit = RUNTIME["limits"]["tool_output_chars"]
                        result.update(answer=answer[:limit], truncated=len(answer) > limit)
                        if not answer.strip() or result['truncated']:
                            result.update(isError=True, error='delegated answer is empty or truncated; '
                                          'inspect the child session before repeating external actions')
                    else:
                        reason = "\n".join(part.text for part in task.status.message.parts
                                           if part.HasField("text"))
                        result["error"] = reason[:500] or "delegated task did not complete successfully"
                        result['isError'] = True
                        failure = MessageToDict(task).get("metadata", {}).get("failure")
                        if isinstance(failure, dict):
                            result["failure"] = {key: value[:100] for key, value in failure.items()
                                                 if key in {"stage", "code", "error_type"}
                                                 and isinstance(value, str)}
                    return json.dumps(result, ensure_ascii=False)
                except (ValueError, KeyError, TypeError, OSError, asyncio.TimeoutError, ParseError, SDKError) as exc:
                    if task_id:
                        try:
                            await invoke(lambda ctx: transport.cancel_task(
                                types.CancelTaskRequest(id=task_id), context=ctx), cleanup=True)
                        except (ValueError, OSError, asyncio.TimeoutError, ParseError, SDKError):
                            pass
                    issue = (str(exc)[:300] if type(exc) is ValueError or
                             isinstance(exc, SDKError) and not isinstance(exc, A2AClientError)
                             else "delegation failed or exceeded the turn deadline")
                    return json.dumps({"sessionId": task_id, "role": arguments["role"],
                                       "isError": True, "error": issue})

        return asyncio.run(execute())

    return {"delegate_task": {"schema": schema, "call": delegate}}


class BoundedHTTPTransport(httpx.AsyncHTTPTransport):
    """Bound responses before the SDK parses JSON; retain its transport and models."""

    async def handle_async_request(self, request):
        response = await super().handle_async_request(request)
        try:
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > RUNTIME["limits"]["history_bytes"] + 4096:
                    raise ValueError("A2A response exceeds the resource limit")
            return httpx.Response(response.status_code, headers={key: value
                for key, value in response.headers.items()
                if key.lower() not in {"content-encoding", "content-length"}}, content=bytes(content))
        finally:
            await response.aclose()

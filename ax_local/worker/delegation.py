"""Expose allowed AX roles as a bounded A2A delegation tool to the model."""

import asyncio
import hashlib
import json
import os
import re
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


def make_tools(environ: dict | None = None, *, probe: dict | None = None) -> dict:
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
        "description": "Start an AX agent or continue its unfinished task with the same role and session_id. "
                       "Continuation restores that session's sandbox and tool context. Inspect outcome and evidence. "
                       "Give corrective instructions for remaining work; do not repeat uncertain external actions. "
                       "Give it the task and all required context explicitly. Its workspace and "
                       "conversation are separate. Select by the role descriptions below. "
                       "For code review, send requirements, the diff and relevant source, test "
                       "results, or a readable repository/ref/PR; a local path alone is insufficient. "
                       "Use its result in your final answer. Available specialists:\n" + catalog,
        "parameters": {"type": "object", "properties": {
            "role": {"type": "string", "enum": allowed},
            "task": {"type": "string", "description": "Task or corrective instructions with requirements and relevant input data"},
            "session_id": {"type": "string", "pattern": "^[0-9a-f]{16}$",
                           "description": "Existing unfinished child sessionId; omit only for a new task"}},
            "required": ["role", "task"], "additionalProperties": False}}}
    poll_interval = float(environ.get("AX_DELEGATION_POLL_SECONDS", "0.5"))

    def delegate(arguments, *, timeout_seconds: float, call_id: str) -> str:
        if not isinstance(arguments, dict) or not {'role', 'task'} <= set(arguments) \
                or set(arguments) - {'role', 'task', 'session_id'} \
                or ('session_id' in arguments and (probe is not None or not isinstance(arguments['session_id'], str)
                    or not re.fullmatch(r'[0-9a-f]{16}', arguments['session_id']))) \
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
                if probe is not None:
                    params.message.metadata.update({'harness2': probe})
                if arguments.get('session_id'):
                    params.message.task_id = arguments['session_id']

                async def send(context):
                    params.message.metadata.update({'timeout_seconds': deadline - time.monotonic()})
                    return await transport.send_message(params, context=context)

                try:
                    try:
                        response = await invoke(send)
                    except (A2AClientError, asyncio.TimeoutError) as exc:
                        # Only retry transport failures; the message ID deduplicates side effects.
                        if not isinstance(exc, asyncio.TimeoutError) and not isinstance(
                                exc.__cause__, httpx.RequestError):
                            raise
                        response = await invoke(send)
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
                    metadata = MessageToDict(task).get('metadata', {})
                    outcome = metadata.get('outcome')
                    result['outcome'] = outcome if isinstance(outcome, dict) else {
                        'status': 'failed', 'reason': 'missing_or_invalid_outcome'}
                    result['resumable'] = metadata.get('resumable') is True
                    if probe is not None:
                        result['activity'] = MessageToDict(task).get('metadata', {}).get('activity', [])
                    if task.status.state == types.TASK_STATE_COMPLETED:
                        answer = "\n".join(part.text for artifact in task.artifacts
                                           for part in artifact.parts if part.HasField("text"))
                        limit = RUNTIME["limits"]["tool_output_chars"]
                        result.update(answer=answer[:limit], truncated=len(answer) > limit)
                        if result['outcome'].get('status') != 'completed':
                            result.update(isError=True, error='delegated task is incomplete; inspect outcome and '
                                          'continue with the same role and session_id when resumable')
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
                    return json.dumps({"sessionId": task_id or arguments.get('session_id'), "role": arguments["role"],
                                       "isError": True, "error": issue})

        return asyncio.run(execute())

    return {"delegate_task": {"schema": schema, "call": delegate}}


def run_probe(task: str, prompt: str, *, timeout_seconds: float, call_id: str,
              environ: dict | None = None) -> dict:
    """Runtime-only call; this capability is never exposed as a model tool."""
    environ = os.environ if environ is None else environ
    role = environ['AX_HARNESS2_ROLE']
    tools = make_tools({'AX_DELEGATION_URL': environ['AX_HARNESS2_URL'],
        'AX_DELEGATION_TOKEN': environ['AX_HARNESS2_TOKEN'],
        'AX_DELEGATION_ROLES': json.dumps([role]), 'AX_DELEGATION_POLL_SECONDS': '0.1'},
        probe={'prompt': prompt, 'timeout_seconds': min(300, timeout_seconds)})
    return json.loads(tools['delegate_task']['call']({'role': role, 'task': task},
        timeout_seconds=timeout_seconds, call_id=call_id))


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

"""Expose allowed AX roles as a bounded A2A delegation tool to the model."""

import hashlib
import json
import os
import time
import uuid
from urllib import error, request
from urllib.parse import urlsplit

from popot_agents.runtime_config import RUNTIME


def make_tools(environ: dict | None = None) -> dict:
    environ = os.environ if environ is None else environ
    url, token = environ.get("AX_DELEGATION_URL"), environ.get("AX_DELEGATION_TOKEN")
    allowed = json.loads(environ.get("AX_DELEGATION_ROLES", "[]"))
    if not url or not token or not allowed:
        return {}
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname \
            or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("invalid AX delegation endpoint")
    schema = {"type": "function", "function": {
        "name": "delegate_task",
        "description": "Start a separate AX agent with an allowed role and wait for its result. "
                       "Give it the task and all required context explicitly. Its workspace and "
                       "conversation are separate. Use its result in your final answer.",
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
            return json.dumps({"error": "delegate_task requires an allowed role and a nonempty task"})
        deadline = time.monotonic() + timeout_seconds
        endpoint = url.rstrip("/") + "/a2a/" + arguments["role"]
        task_id = None

        def rpc(method, params, *, cleanup=False):
            remaining = 2 if cleanup else deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("delegation deadline exceeded")
            body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params}
            call = request.Request(endpoint, data=json.dumps(body).encode(), headers={
                "Content-Type": "application/json", "A2A-Version": "1.0",
                "Authorization": "Bearer " + token}, method="POST")
            with request.urlopen(call, timeout=min(5, remaining)) as response:
                raw = response.read(RUNTIME["limits"]["history_bytes"] + 4096)
                result = json.loads(raw)
            if "error" in result:
                raise ValueError(result["error"].get("message", "A2A request rejected"))
            return result["result"]

        try:
            params = {"message": {
                "messageId": hashlib.sha256(call_id.encode()).hexdigest(), "role": "ROLE_USER",
                "parts": [{"text": arguments["task"]}]},
                "configuration": {"blocking": False}}
            try:
                task = rpc("SendMessage", params)["task"]
            except (error.URLError, OSError):
                # A response can be lost after task creation. Reuse the message ID.
                task = rpc("SendMessage", params)["task"]
            task_id = task["id"]
            while task["status"]["state"] in {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("delegation deadline exceeded")
                time.sleep(min(poll_interval, remaining))
                task = rpc("GetTask", {"id": task_id})
            state = task["status"]["state"]
            result = {"sessionId": task_id, "role": arguments["role"], "state": state}
            if state == "TASK_STATE_COMPLETED":
                answer = "\n".join(part["text"] for artifact in task.get("artifacts", [])
                                   for part in artifact.get("parts", []) if "text" in part)
                limit = RUNTIME["limits"]["tool_output_chars"]
                result.update(answer=answer[:limit], truncated=len(answer) > limit)
            else:
                message = task["status"].get("message", {})
                reason = "\n".join(part["text"] for part in message.get("parts", [])
                                   if isinstance(part, dict) and isinstance(part.get("text"), str))
                result["error"] = reason[:500] or "delegated task did not complete successfully"
                failure = task.get("metadata", {}).get("failure")
                if isinstance(failure, dict):
                    result["failure"] = {key: value[:100] for key, value in failure.items()
                                         if key in {"stage", "code", "error_type"}
                                         and isinstance(value, str)}
            return json.dumps(result, ensure_ascii=False)
        except (ValueError, KeyError, TypeError, error.URLError, OSError) as exc:
            if task_id:
                try:
                    rpc("CancelTask", {"id": task_id}, cleanup=True)
                except (ValueError, KeyError, TypeError, error.URLError, OSError):
                    pass
            issue = (str(exc)[:300] if type(exc) is ValueError
                     else "delegation failed or exceeded the turn deadline")
            return json.dumps({"sessionId": task_id, "role": arguments["role"], "error": issue})

    return {"delegate_task": {"schema": schema, "call": delegate}}

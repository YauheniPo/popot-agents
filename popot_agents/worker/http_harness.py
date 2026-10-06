"""OpenAI-compatible chat harness with a bounded local tool loop."""

import json
import os
import sys
import time
import uuid
from urllib import error, request
from urllib.parse import urlsplit

from popot_agents.runtime_config import RUNTIME, validate_model_parameters
from popot_agents.skills import prepare_role_config
from popot_agents.tools import TOOL_SCHEMAS, ToolExecutionError, ToolInputError, execute_tool
from . import mcp_client


def _trace(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False),
          file=sys.stderr, flush=True)


def _trace_arguments(name: str, arguments: dict) -> dict:
    if name == "calculate":
        return {"expression": arguments.get("expression")}
    if name == "utc_time":
        return {}
    if name == "read_file":
        return {"path": arguments.get("path")}
    if name == "write_file":
        return {"path": arguments.get("path"),
                "content_chars": len(arguments["content"]) if isinstance(arguments.get("content"), str) else None}
    if name in {"git_clone", "download_file"}:
        try:
            host = urlsplit(arguments.get("url", "")).hostname
        except (ValueError, TypeError, AttributeError):
            host = None
        return {"url_host": host,
                **{key: arguments.get(key) for key in ("directory", "path") if key in arguments}}
    if name == "bash":
        return {"command_chars": len(arguments["command"]) if isinstance(arguments.get("command"), str) else None}
    return {"argument_keys": sorted(arguments)}


def _model_request(call, *, round_number, messages, schemas, remaining_seconds):
    """Retry only completion generation; tools run after a complete response."""
    attempts = 1 + RUNTIME["model"]["request_retries"]
    for attempt in range(1, attempts + 1):
        request_timeout = min(RUNTIME["timeouts"]["model_request_seconds"], remaining_seconds())
        started_at = time.monotonic()
        _trace("model_request", round=round_number, attempt=attempt, message_count=len(messages),
               timeout_seconds=round(request_timeout, 3),
               available_tools=[schema["function"]["name"] for schema in schemas])
        print(f"LLM request started round={round_number} attempt={attempt}", file=sys.stderr, flush=True)
        retryable = False
        try:
            with request.urlopen(call, timeout=request_timeout) as response:
                result = json.load(response)
        except error.HTTPError as exc:
            code = exc.code
            exc.close()
            failure = f"model endpoint returned HTTP {code}"
            retryable = code in {502, 503, 504}
            _trace("model_http_error", round=round_number, attempt=attempt, status=code)
        except (TimeoutError, error.URLError) as exc:
            if isinstance(exc, TimeoutError) or isinstance(exc.reason, TimeoutError):
                elapsed = time.monotonic() - started_at
                _trace("model_timeout", round=round_number, attempt=attempt,
                       elapsed_seconds=round(elapsed, 1), timeout_seconds=round(request_timeout, 3))
                failure = (f"model request timed out (round={round_number}, "
                           f"socket timeout={request_timeout:.1f}s, elapsed={elapsed:.1f}s, "
                           f"attempt={attempt}/{attempts})")
                retryable = True
            else:
                failure = "model endpoint is unreachable"
        else:
            print(f"LLM response received after {time.monotonic() - started_at:.1f}s",
                  file=sys.stderr, flush=True)
            return result
        if not retryable or attempt == attempts:
            raise RuntimeError(failure) from None
        remaining = remaining_seconds()
        _trace("model_retry", round=round_number, next_attempt=attempt + 1,
               reason=failure, remaining_seconds=round(remaining, 3))


def run_http(task: str | list[dict[str, str]], role_config: dict | None = None,
             *, extra_tools: dict | None = None) -> str:
    base_url = os.environ.get("HARNESS_BASE_URL", "").rstrip("/")
    model = os.environ.get("HARNESS_MODEL", "")
    if not base_url.startswith(("http://", "https://")) or not model:
        raise ValueError("HARNESS_BASE_URL and HARNESS_MODEL are required")
    turn_timeout = float(os.getenv("HARNESS_TURN_TIMEOUT_SECONDS",
                                   str(RUNTIME["worker"]["default_timeout_seconds"])))
    if turn_timeout <= 0:
        raise ValueError("HARNESS_TURN_TIMEOUT_SECONDS must be positive")
    deadline = time.monotonic() + turn_timeout

    def remaining_seconds() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("turn deadline exceeded")
        return remaining

    messages = [{"role": "user", "content": task}] if isinstance(task, str) else list(task)
    role_config = prepare_role_config(role_config)
    instructions = role_config.get("instructions", "")
    allowed = role_config.get("tools", [])
    mcp_servers = role_config.get("mcpServers", {})
    mcp_tools = (mcp_client.discover_tools(mcp_servers, timeout_seconds=remaining_seconds())
                 if mcp_servers else {})
    schemas = [TOOL_SCHEMAS[name] for name in allowed]
    schemas.extend(item["schema"] for item in mcp_tools.values())
    extra_tools = extra_tools or {}
    turn_id = uuid.uuid4().hex
    schemas.extend(item["schema"] for item in extra_tools.values())
    if instructions:
        messages.insert(0, {"role": "system", "content": instructions})
    headers = {"Content-Type": "application/json"}
    model_parameters = {key: value for key, value in RUNTIME["model"].items()
                        if key in {"temperature", "max_tokens"} and value is not None}
    model_parameters.update(validate_model_parameters(
        json.loads(os.getenv("HARNESS_MODEL_PARAMETERS_JSON", "{}"))))
    key_env = os.environ.get("HARNESS_API_KEY_ENV")
    if key_env:
        key = os.environ.get(key_env)
        if not key:
            raise ValueError(f"required environment variable is missing: {key_env}")
        headers["Authorization"] = f"Bearer {key}"
    for round_number in range(1, role_config.get("max_tool_rounds",
                                             RUNTIME["model"]["default_max_tool_rounds"]) + 1):
        body = {"model": model, "messages": messages}
        body.update(model_parameters)
        if schemas:
            body["tools"] = schemas
        call = request.Request(
            f"{base_url}/chat/completions", data=json.dumps(body).encode("utf-8"),
            headers=headers, method="POST",
        )
        result = _model_request(call, round_number=round_number, messages=messages,
                                schemas=schemas, remaining_seconds=remaining_seconds)
        remaining_seconds()
        try:
            message = result["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("model endpoint returned no answer") from exc
        calls = message.get("tool_calls") or []
        _trace("model_response", round=round_number, tool_calls=len(calls))
        if calls:
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                _trace("assistant_message", round=round_number, content=content)
            messages.append(message)
            for tool_call in calls:
                remaining_seconds()
                name = None
                tool_started_at = time.monotonic()
                try:
                    function = tool_call["function"]
                    name = function["name"]
                    call_id = tool_call["id"]
                    if not isinstance(name, str) or not isinstance(call_id, str) or not call_id:
                        raise ValueError("tool name and call ID must be nonempty strings")
                    if name not in extra_tools and name not in mcp_tools and name not in allowed:
                        raise ValueError(f"tool is not allowed: {name}")
                    try:
                        arguments = json.loads(function["arguments"])
                    except (json.JSONDecodeError, TypeError) as exc:
                        raise ToolInputError("tool arguments must be valid JSON encoding an object") from exc
                    if not isinstance(arguments, dict):
                        raise ToolInputError("tool arguments must be an object")
                    _trace("tool_call", round=round_number, name=name,
                           call_id=tool_call["id"],
                           arguments=_trace_arguments(name, arguments))
                    if name in extra_tools:
                        output = extra_tools[name]["call"](
                            arguments, timeout_seconds=remaining_seconds(),
                            call_id=turn_id + ":" + tool_call["id"])
                    elif name in mcp_tools:
                        selected = mcp_tools[name]
                        output = mcp_client.call_tool(
                            mcp_servers[selected["server"]], selected["native_name"], arguments,
                            timeout_seconds=remaining_seconds())
                    else:
                        output = execute_tool(name, arguments, allowed,
                                              timeout_seconds=remaining_seconds())
                except (ToolInputError, ToolExecutionError, mcp_client.MCPToolError) as exc:
                    _trace("tool_error", round=round_number, name=name, call_id=call_id,
                           error_type=type(exc).__name__, recoverable=True)
                    output = json.dumps({
                        "isError": True, "error": str(exc),
                        "next_step": "Correct the arguments or choose another permitted step. "
                                     "Inspect any partial effects before repeating a write. "
                                     "Do not claim success without verification.",
                    }, ensure_ascii=False)
                except (KeyError, TypeError, json.JSONDecodeError, ValueError) as exc:
                    _trace("tool_error", round=round_number, name=name,
                           error_type=type(exc).__name__)
                    raise RuntimeError(f"invalid tool call: {exc}") from exc
                except Exception as exc:
                    _trace("tool_error", round=round_number, name=name,
                           error_type=type(exc).__name__)
                    raise
                result_fields = ({"output": output} if name in {"calculate", "utc_time"}
                                 else {"output_chars": len(output)})
                _trace("tool_result", round=round_number, name=name, call_id=call_id,
                       elapsed_seconds=round(time.monotonic() - tool_started_at, 1),
                       **result_fields)
                messages.append({"role": "tool", "tool_call_id": call_id, "content": output})
            continue
        answer = message.get("content")
        if not isinstance(answer, str) or not answer.strip():
            raise RuntimeError("model endpoint returned an empty answer")
        _trace("assistant_answer", round=round_number, content=answer.strip())
        remaining_seconds()
        return answer.strip()
    raise RuntimeError("model exceeded the tool-call limit")


def main() -> None:
    try:
        role_config = json.loads(os.getenv("HARNESS_ROLE_JSON", "{}"))
        print(run_http(sys.stdin.read(), role_config), flush=True)
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f"HTTP harness failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()

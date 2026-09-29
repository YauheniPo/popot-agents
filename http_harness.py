"""OpenAI-compatible chat harness with a bounded local tool loop."""

import json
import os
import sys
from urllib import error, request

import mcp_client
from harness_tools import TOOL_SCHEMAS, execute_tool


def run_http(task: str | list[dict[str, str]], role_config: dict | None = None) -> str:
    base_url = os.environ.get("HARNESS_BASE_URL", "").rstrip("/")
    model = os.environ.get("HARNESS_MODEL", "")
    if not base_url.startswith(("http://", "https://")) or not model:
        raise ValueError("HARNESS_BASE_URL and HARNESS_MODEL are required")
    messages = [{"role": "user", "content": task}] if isinstance(task, str) else list(task)
    role_config = role_config or {}
    instructions = role_config.get("instructions", "")
    allowed = role_config.get("tools", [])
    mcp_servers = role_config.get("mcpServers", {})
    mcp_tools = mcp_client.discover_tools(mcp_servers) if mcp_servers else {}
    schemas = [TOOL_SCHEMAS[name] for name in allowed]
    schemas.extend(item["schema"] for item in mcp_tools.values())
    if instructions:
        messages.insert(0, {"role": "system", "content": instructions})
    headers = {"Content-Type": "application/json"}
    key_env = os.environ.get("HARNESS_API_KEY_ENV")
    if key_env:
        key = os.environ.get(key_env)
        if not key:
            raise ValueError(f"required environment variable is missing: {key_env}")
        headers["Authorization"] = f"Bearer {key}"
    for _ in range(role_config.get("max_tool_rounds", 5)):
        body = {"model": model, "messages": messages}
        if schemas:
            body["tools"] = schemas
        call = request.Request(
            f"{base_url}/chat/completions", data=json.dumps(body).encode("utf-8"),
            headers=headers, method="POST",
        )
        try:
            with request.urlopen(call, timeout=45) as response:
                result = json.load(response)
        except error.HTTPError as exc:
            raise RuntimeError(f"model endpoint returned HTTP {exc.code}") from exc
        except error.URLError as exc:
            raise RuntimeError("model endpoint is unreachable") from exc
        try:
            message = result["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("model endpoint returned no answer") from exc
        calls = message.get("tool_calls") or []
        if calls:
            messages.append(message)
            for tool_call in calls:
                try:
                    function = tool_call["function"]
                    name = function["name"]
                    arguments = json.loads(function["arguments"])
                    if name in mcp_tools:
                        selected = mcp_tools[name]
                        output = mcp_client.call_tool(
                            mcp_servers[selected["server"]], selected["native_name"], arguments)
                    else:
                        output = execute_tool(name, arguments, allowed)
                    call_id = tool_call["id"]
                except (KeyError, TypeError, json.JSONDecodeError, ValueError) as exc:
                    raise RuntimeError(f"invalid tool call: {exc}") from exc
                messages.append({"role": "tool", "tool_call_id": call_id, "content": output})
            continue
        answer = message.get("content")
        if not isinstance(answer, str) or not answer.strip():
            raise RuntimeError("model endpoint returned an empty answer")
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

"""Connect role-selected MCP stdio and authenticated HTTPS servers."""

import asyncio
import json
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from popot_agents.runtime_config import RUNTIME
from popot_agents.tools import MAX_TOOL_OUTPUT, safe_tool_env


class MCPToolError(RuntimeError):
    """The server returned isError: the model may correct its tool arguments."""


def _parameters(config: dict):
    from mcp import StdioServerParameters

    command = config["command"]
    return StdioServerParameters(command=sys.executable,
                                 args=["-I", str(Path(__file__).with_name("tool_launcher.py")),
                                       *command],
                                 env=safe_tool_env())


@asynccontextmanager
async def _streams(config: dict):
    if "url" in config:
        token_name = config["bearer_token_env"]
        token = safe_tool_env().get(token_name)
        if not token:
            raise ValueError(f"required MCP environment variable is missing or not selected: {token_name}")
        import httpx
        from mcp.client.streamable_http import streamable_http_client

        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"},
                                     follow_redirects=False) as client:
            async with streamable_http_client(config["url"], http_client=client) as (read, write, _):
                yield read, write
    else:
        from mcp.client.stdio import stdio_client

        async with stdio_client(_parameters(config)) as streams:
            yield streams


def _error_text(config: dict, exc: Exception) -> str:
    text = str(exc)
    token = safe_tool_env().get(config.get("bearer_token_env", ""))
    return text.replace(token, "[REDACTED]") if token else text


async def _discover_one(alias: str, config: dict) -> dict:
    async with _streams(config) as (read, write):
        from mcp import ClientSession

        async with ClientSession(read, write) as session:
            await session.initialize()
            listed, cursors = {}, set()
            cursor = None
            while True:
                page = await session.list_tools(cursor=cursor) if cursor else await session.list_tools()
                listed.update({tool.name: tool for tool in page.tools})
                cursor = getattr(page, "nextCursor", None)
                if not cursor:
                    break
                if cursor in cursors:
                    raise RuntimeError("MCP server repeated a tools cursor")
                cursors.add(cursor)
    missing = set(config["tools"]) - listed.keys()
    if missing:
        raise RuntimeError(f"MCP server {alias} does not expose: {', '.join(sorted(missing))}")
    result = {}
    for native_name in config["tools"]:
        tool = listed[native_name]
        name = f"mcp__{alias}__{native_name}"
        result[name] = {
            "server": alias,
            "native_name": native_name,
            "schema": {"type": "function", "function": {
                "name": name,
                "description": tool.description or f"{alias}: {native_name}",
                "parameters": tool.inputSchema,
            }},
        }
    return result


def discover_tools(servers: dict, *, timeout_seconds: float | None = None) -> dict:
    discovered = {}
    deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
    for alias, config in servers.items():
        try:
            timeout = RUNTIME["timeouts"]["mcp_discovery_seconds"]
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                timeout = min(timeout, remaining)
            discovered.update(asyncio.run(asyncio.wait_for(
                _discover_one(alias, config), timeout)))
        except TimeoutError as exc:
            raise RuntimeError(f"MCP server {alias} timed out during discovery") from exc
        except Exception as exc:
            raise RuntimeError(f"MCP server {alias} failed during discovery: {_error_text(config, exc)}") from None
    return discovered


async def _call_one(config: dict, native_name: str, arguments: dict) -> str:
    async with _streams(config) as (read, write):
        from mcp import ClientSession

        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(native_name, arguments=arguments)
    parts = []
    for item in result.content:
        kind = getattr(item, "type", None)
        if kind == "text":
            parts.append(item.text)
        elif kind == "resource":
            resource = item.resource
            parts.append(f"[{resource.uri}]\n" + getattr(
                resource, "text", "[binary resource omitted]"))
        elif kind == "resource_link":
            parts.append(f"Resource link (content not loaded): {item.uri}")
    output = "\n".join(parts) or json.dumps(
        getattr(result, "structuredContent", None) or {}, ensure_ascii=False)
    if result.isError:
        raise MCPToolError(output)
    return output[:MAX_TOOL_OUTPUT] + ("\n[output truncated]" if len(output) > MAX_TOOL_OUTPUT else "")


def call_tool(config: dict, native_name: str, arguments: dict,
              *, timeout_seconds: float | None = None) -> str:
    try:
        timeout = RUNTIME["timeouts"]["mcp_tool_seconds"]
        if timeout_seconds is not None:
            timeout = min(timeout, timeout_seconds)
        return asyncio.run(asyncio.wait_for(
            _call_one(config, native_name, arguments), timeout))
    except MCPToolError as exc:
        # Redact before truncation so a token cut in the middle cannot leak.
        raise MCPToolError(_error_text(config, exc)[:MAX_TOOL_OUTPUT]) from None
    except TimeoutError as exc:
        raise RuntimeError("MCP tool call timed out") from exc
    except Exception as exc:
        raise RuntimeError(f"MCP tool {native_name} failed: {_error_text(config, exc)}") from None

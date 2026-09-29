"""Connect role-selected public MCP stdio servers inside the worker."""

import asyncio
import json
import sys

from harness_tools import MAX_TOOL_OUTPUT, safe_tool_env


def _parameters(config: dict):
    from mcp import StdioServerParameters

    command = config["command"]
    return StdioServerParameters(command=sys.executable,
                                 args=["/app/tool_launcher.py", *command],
                                 env=safe_tool_env())


async def _discover_one(alias: str, config: dict) -> dict:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async with stdio_client(_parameters(config)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = {tool.name: tool for tool in (await session.list_tools()).tools}
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


def discover_tools(servers: dict) -> dict:
    discovered = {}
    for alias, config in servers.items():
        try:
            discovered.update(asyncio.run(asyncio.wait_for(_discover_one(alias, config), 20)))
        except TimeoutError as exc:
            raise RuntimeError(f"MCP server {alias} timed out during discovery") from exc
        except Exception as exc:
            raise RuntimeError(f"MCP server {alias} failed during discovery: {exc}") from exc
    return discovered


async def _call_one(config: dict, native_name: str, arguments: dict) -> str:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async with stdio_client(_parameters(config)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(native_name, arguments=arguments)
    parts = [item.text for item in result.content if getattr(item, "type", None) == "text"]
    output = "\n".join(parts) or json.dumps(
        getattr(result, "structuredContent", None) or {}, ensure_ascii=False)
    if result.isError:
        raise RuntimeError(output[:MAX_TOOL_OUTPUT])
    return output[:MAX_TOOL_OUTPUT] + ("\n[output truncated]" if len(output) > MAX_TOOL_OUTPUT else "")


def call_tool(config: dict, native_name: str, arguments: dict) -> str:
    try:
        return asyncio.run(asyncio.wait_for(_call_one(config, native_name, arguments), 30))
    except TimeoutError as exc:
        raise RuntimeError("MCP tool call timed out") from exc
    except Exception as exc:
        raise RuntimeError(f"MCP tool {native_name} failed: {exc}") from exc

"""Connect role-selected MCP stdio and authenticated HTTPS servers."""

import asyncio
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from popot_agents.runtime_config import RUNTIME
from popot_agents.progress import trace_event
from popot_agents.tools import MAX_TOOL_OUTPUT, preserve_tool_output, safe_tool_env


class MCPToolError(RuntimeError):
    """The server returned isError: the model may correct its tool arguments."""


class MCPTransportError(MCPToolError):
    """No confirmed response; external effects must be checked before retrying."""


def _bearer_token(config):
    name = config.get('bearer_token_env', '')
    if name == 'AX_HARNESS2_GITHUB_TOKEN' \
            and config.get('url') == 'https://api.githubcopilot.com/mcp/x/all/readonly':
        return os.environ.get(name)
    return safe_tool_env().get(name)


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
        token = _bearer_token(config)
        if not token:
            raise ValueError(f"required MCP environment variable is missing or not selected: {token_name}")
        import httpx2
        from mcp.client.streamable_http import streamable_http_client

        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"},
                                     follow_redirects=False,
                                     # Outer discovery/tool deadlines still apply.
                                     timeout=httpx2.Timeout(RUNTIME['timeouts']['mcp_http_seconds'],
                                         read=RUNTIME['timeouts']['mcp_sse_read_seconds'])) as client:
            async with streamable_http_client(config["url"], http_client=client) as (read, write):
                yield read, write
    else:
        from mcp.client.stdio import stdio_client

        async with stdio_client(_parameters(config)) as streams:
            yield streams


def _error_text(config: dict, exc: Exception) -> str:
    def details(error: BaseException) -> str:
        # anyio uses native ExceptionGroup or its Python 3.10 backport.
        children = getattr(error, "exceptions", ())
        if children:
            return "; ".join(details(child) for child in children)
        return f"{type(error).__name__}: {error}"

    text = details(exc)
    token = _bearer_token(config)
    text = text.replace(token, "[REDACTED]") if token else text
    return text[:MAX_TOOL_OUTPUT]


async def _discover_one(alias: str, config: dict) -> dict:
    from mcp import Client

    # The SDK negotiates modern MCP and falls back for older tool servers.
    async with Client(_streams(config)) as client:
        listed, cursors = {}, set()
        cursor = None
        while True:
            page = await client.list_tools(cursor=cursor)
            listed.update({tool.name: tool for tool in page.tools})
            cursor = page.next_cursor
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
                "parameters": tool.input_schema,
            }},
        }
    return result


def _retryable_discovery_error(exc: Exception) -> bool:
    try:
        import httpx2 as httpx
    except ImportError:
        return False  # Preserve the original configuration/dependency failure.

    children = getattr(exc, 'exceptions', ())
    if children:
        return all(_retryable_discovery_error(child) for child in children)
    return isinstance(exc, (httpx.NetworkError, httpx.TimeoutException,
                            httpx.RemoteProtocolError))


async def _discover_with_retry(alias: str, config: dict) -> dict:
    policy = RUNTIME['mcp']
    for attempt in range(1, policy['discovery_attempts'] + 1):
        trace_event('mcp_discovery_started', server=alias, attempt=attempt)
        try:
            result = await _discover_one(alias, config)
        except Exception as exc:
            if 'url' not in config or attempt == policy['discovery_attempts'] or not _retryable_discovery_error(exc):
                raise
            delay = policy['discovery_backoff_seconds'] * attempt
            trace_event('mcp_discovery_retry', server=alias, attempt=attempt,
                        delay_seconds=delay, error=_error_text(config, exc))
            # Only handshake/list_tools are replayed. Sleep and connection
            # cleanup share the caller's existing discovery deadline.
            await asyncio.sleep(delay)
        else:
            trace_event('mcp_discovery_completed', server=alias, attempt=attempt,
                        tool_count=len(result))
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
                _discover_with_retry(alias, config), timeout)))
        except (TimeoutError, asyncio.TimeoutError) as exc:
            trace_event('mcp_discovery_failed', server=alias, reason='deadline_exceeded')
            raise RuntimeError(f"MCP server {alias} timed out during discovery") from exc
        except Exception as exc:
            trace_event('mcp_discovery_failed', server=alias, error=_error_text(config, exc))
            raise RuntimeError(f"MCP server {alias} failed during discovery: {_error_text(config, exc)}") from None
    return discovered


async def _call_one(config: dict, native_name: str, arguments: dict) -> str:
    from mcp import Client

    result = None
    try:
        async with Client(_streams(config)) as client:
            result = await client.call_tool(native_name, arguments=arguments)
    except (Exception, asyncio.CancelledError) as exc:
        if result is None:
            raise
        # A completed RPC must not become an unknown write when closing its
        # transport fails or the outer deadline cancels cleanup. Preserve the
        # received result, including is_error; cancellation before it propagates.
        trace_event('mcp_cleanup_failed', tool=native_name,
                    error=_error_text(config, exc), result_received=True)
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
        getattr(result, "structured_content", None) or {}, ensure_ascii=False)
    if result.is_error:
        raise MCPToolError(output)
    return preserve_tool_output(output)


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
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise MCPTransportError("MCP tool call timed out; outcome is unknown") from None
    except Exception as exc:
        raise MCPTransportError(f"MCP tool {native_name} failed: {_error_text(config, exc)}") from None

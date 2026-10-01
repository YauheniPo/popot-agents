"""Small, explicit tool registry for the HTTP harness."""

import ast
import json
import operator
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from popot_agents.runtime_config import RUNTIME

WORKSPACE_ROOT = Path(os.getenv("TOOL_WORKSPACE_ROOT", "/workspace"))
MAX_TOOL_OUTPUT = RUNTIME["limits"]["tool_output_chars"]


TOOL_SCHEMAS = {
    "calculate": {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Evaluate basic arithmetic with +, -, *, / and parentheses.",
            "parameters": {
                "type": "object", "properties": {"expression": {"type": "string"}},
                "required": ["expression"], "additionalProperties": False,
            },
        },
    },
    "utc_time": {
        "type": "function",
        "function": {
            "name": "utc_time",
            "description": "Get the current UTC date and time in ISO 8601 format.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
}


def _tool(name, description, properties, required):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": required, "additionalProperties": False},
    }}


TOOL_SCHEMAS.update({
    "bash": _tool("bash", "Run a bash command in the writable workspace and return output and exit code.",
                  {"command": {"type": "string"}}, ["command"]),
    "read_file": _tool("read_file", "Read a UTF-8 file inside the workspace.",
                       {"path": {"type": "string"}}, ["path"]),
    "write_file": _tool("write_file", "Write a UTF-8 file inside the workspace, creating parent directories.",
                        {"path": {"type": "string"}, "content": {"type": "string"}},
                        ["path", "content"]),
    "git_clone": _tool("git_clone", "Clone a public HTTPS Git repository into the workspace.",
                       {"url": {"type": "string"}, "directory": {"type": "string"}},
                       ["url", "directory"]),
    "download_file": _tool("download_file", "Download a public HTTP(S) file into the workspace, within the configured size limit.",
                           {"url": {"type": "string"}, "path": {"type": "string"}},
                           ["url", "path"]),
})

_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub,
           ast.Mult: operator.mul, ast.Div: operator.truediv}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def safe_tool_env() -> dict[str, str]:
    """Do not forward model credentials or harness configuration to child tools."""
    return ({key: value for key, value in os.environ.items()
             if key in {"PATH", "LANG", "LC_ALL", "TERM", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
            | {"HOME": str(WORKSPACE_ROOT), "GIT_TERMINAL_PROMPT": "0"})


def workspace_path(path: str) -> Path:
    if not isinstance(path, str) or not path or len(path) > 300:
        raise ValueError("path is required")
    root = WORKSPACE_ROOT.resolve()
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root) or candidate == root:
        raise ValueError("path is outside workspace")
    return candidate


def _public_url(value: str, https_only: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError("valid URL is required")
    parsed = urlsplit(value)
    schemes = {"https"} if https_only else {"http", "https"}
    if parsed.scheme not in schemes or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("public HTTPS URL is required" if https_only else "public HTTP(S) URL is required")
    return value


def _run_command(command: list[str],
                 timeout: float = RUNTIME["timeouts"]["tool_command_seconds"]) -> str:
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as output:
        try:
            options = {"cwd": WORKSPACE_ROOT, "env": safe_tool_env(),
                       "stdout": output, "stderr": subprocess.STDOUT,
                       "timeout": timeout, "check": False}
            if os.geteuid() == 0:
                options.update(user=10001, group=10001)
            completed = subprocess.run(command, **options)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("command timed out") from exc
        output.seek(0)
        snippet = output.read(MAX_TOOL_OUTPUT + 1)
    if len(snippet) > MAX_TOOL_OUTPUT:
        snippet = snippet[:MAX_TOOL_OUTPUT] + "\n[output truncated]"
    return f"exit_code={completed.returncode}\n{snippet}"


def _file_action(action: str, arguments: dict, timeout_seconds: float | None = None) -> str:
    options = {"input": json.dumps(arguments, ensure_ascii=False), "text": True,
               "capture_output": True,
               "timeout": min(RUNTIME["timeouts"]["file_action_seconds"], timeout_seconds)
               if timeout_seconds is not None else RUNTIME["timeouts"]["file_action_seconds"],
               "check": False,
               "env": safe_tool_env() | {"TOOL_WORKSPACE_ROOT": str(WORKSPACE_ROOT)}}
    if os.geteuid() == 0:
        options.update(user=10001, group=10001)
    completed = subprocess.run(
        [sys.executable, "-m", "popot_agents.worker.file_worker", action], **options)
    if completed.returncode != 0:
        raise RuntimeError(f"{action} failed: {completed.stderr.strip()[:300]}")
    return completed.stdout


def _evaluate(node):
    if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
        value = node.value
    elif isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        value = _BINARY[type(node.op)](_evaluate(node.left), _evaluate(node.right))
    elif isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        value = _UNARY[type(node.op)](_evaluate(node.operand))
    else:
        raise ValueError("unsupported expression")
    if not -1e12 <= value <= 1e12:
        raise ValueError("result is out of range")
    return value


def execute_tool(name: str, arguments: dict, allowed: list[str],
                 *, timeout_seconds: float | None = None) -> str:
    if name not in allowed or name not in TOOL_SCHEMAS:
        raise ValueError(f"tool is not allowed: {name}")
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be an object")
    if name == "bash":
        if set(arguments) != {"command"} or not isinstance(arguments["command"], str) \
                or len(arguments["command"]) > 4000:
            raise ValueError("bash requires a command up to 4000 characters")
        timeout = RUNTIME["timeouts"]["tool_command_seconds"]
        return _run_command(["bash", "--noprofile", "--norc", "-c", arguments["command"]],
                            timeout=min(timeout, timeout_seconds) if timeout_seconds is not None else timeout)
    if name == "read_file":
        if set(arguments) != {"path"}:
            raise ValueError("read_file requires path")
        workspace_path(arguments["path"])
        return _file_action("read", arguments, timeout_seconds)
    if name == "write_file":
        if set(arguments) != {"path", "content"} or not isinstance(arguments["content"], str) \
                or len(arguments["content"].encode("utf-8")) > RUNTIME["limits"]["write_file_bytes"]:
            raise ValueError(f"write_file requires path and content up to {RUNTIME['limits']['write_file_bytes']} bytes")
        workspace_path(arguments["path"])
        return _file_action("write", arguments, timeout_seconds)
    if name == "git_clone":
        if set(arguments) != {"url", "directory"}:
            raise ValueError("git_clone requires url and directory")
        url = _public_url(arguments["url"], https_only=True)
        directory = arguments["directory"]
        if not isinstance(directory, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", directory):
            raise ValueError("directory must be a simple name")
        destination = workspace_path(directory)
        if destination.exists():
            raise ValueError("clone destination already exists")
        result = _run_command(["git", "clone", "--depth", "1", "--", url, str(destination)],
                              timeout=min(RUNTIME["timeouts"]["git_clone_seconds"], timeout_seconds)
                              if timeout_seconds is not None else RUNTIME["timeouts"]["git_clone_seconds"])
        if not result.startswith("exit_code=0\n"):
            raise RuntimeError(result)
        return f"cloned to {destination}\n{result}"
    if name == "download_file":
        if set(arguments) != {"url", "path"}:
            raise ValueError("download_file requires url and path")
        url = _public_url(arguments["url"])
        workspace_path(arguments["path"])
        return _file_action("download", {"url": url, "path": arguments["path"]}, timeout_seconds)
    if name == "utc_time":
        if arguments:
            raise ValueError("utc_time takes no arguments")
        return datetime.now(timezone.utc).isoformat()
    if set(arguments) != {"expression"} or not isinstance(arguments["expression"], str):
        raise ValueError("calculate requires expression")
    expression = arguments["expression"]
    if len(expression) > 100:
        raise ValueError("expression is too long")
    try:
        value = _evaluate(ast.parse(expression, mode="eval").body)
    except (SyntaxError, ZeroDivisionError, OverflowError, RecursionError) as exc:
        raise ValueError("invalid arithmetic expression") from exc
    return str(value)

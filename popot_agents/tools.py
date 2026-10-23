"""Small, explicit tool registry for the HTTP harness."""

import ast
import json
import operator
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from popot_agents.runtime_config import RUNTIME
from popot_agents.role_env import validate_role_env_names

WORKSPACE_ROOT = Path(os.getenv("TOOL_WORKSPACE_ROOT", "/workspace"))
MAX_TOOL_OUTPUT = RUNTIME["limits"]["tool_output_chars"]
CLONE_DIRECTORY_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}"


class ToolInputError(ValueError):
    """Correctable, safe-to-report rejection before a tool performs any action."""


class ToolExecutionError(RuntimeError):
    """A completed tool reported failure; inspect effects before choosing another step."""


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
    "read_file": _tool("read_file", "Read a UTF-8 file in pages; offset counts characters from zero.",
                       {"path": {"type": "string"},
                        "offset": {"type": "integer", "minimum": 0},
                        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_TOOL_OUTPUT}}, ["path"]),
    "write_file": _tool("write_file", "Write a UTF-8 file inside the workspace, creating parent directories.",
                        {"path": {"type": "string"}, "content": {"type": "string"}},
                        ["path", "content"]),
    "git_clone": _tool("git_clone", "Clone a public HTTPS Git repository into the workspace. "
                       "For a PR or a pinned revision, pass its commit_sha to fetch, check out "
                       "and verify that commit before reading source or running tests.",
                       {"url": {"type": "string"}, "directory": {
                           "type": "string", "minLength": 1, "maxLength": 80,
                           "pattern": "^" + CLONE_DIRECTORY_PATTERN + "$",
                           "description": "New folder name inside the workspace, e.g. popot-agents. "
                                          "Use only letters, digits, dots, underscores and hyphens; "
                                          "start with a letter or digit. No absolute paths, slashes "
                                          "or existing folders.",
                       }, "commit_sha": {"type": "string", "pattern": "^[0-9a-fA-F]{40}$",
                           "description": "Full commit SHA from repository/PR metadata, not a file blob SHA. "
                                          "On failure, do not use the checkout as reviewed source."}},
                       ["url", "directory"]),
    "download_file": _tool("download_file", "Download a public HTTP(S) file into the workspace, within the configured size limit.",
                           {"url": {"type": "string"}, "path": {"type": "string"}},
                           ["url", "path"]),
})

TOOL_SCHEMAS['read_tool_output'] = _tool(
    'read_tool_output', 'Read a saved .tool-results output in pages; offset counts characters from zero.',
    TOOL_SCHEMAS['read_file']['function']['parameters']['properties'], ['path'])


_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub,
           ast.Mult: operator.mul, ast.Div: operator.truediv}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def safe_tool_env() -> dict[str, str]:
    """Forward baseline variables and only role-selected credentials to tools."""
    names = validate_role_env_names(json.loads(os.getenv("HARNESS_TOOL_ENV_NAMES_JSON", "[]")))
    return ({key: value for key, value in os.environ.items()
             if key in {"PATH", "LANG", "LC_ALL", "TERM", "SSL_CERT_FILE", "SSL_CERT_DIR",
                        "PLAYWRIGHT_BROWSERS_PATH"}
             or key in names}
            | {"HOME": str(WORKSPACE_ROOT), "GIT_TERMINAL_PROMPT": "0",
               "NPM_CONFIG_PREFIX": str(WORKSPACE_ROOT / '.local'),
               "PATH": os.pathsep.join(dict.fromkeys([
                   *os.environ.get('PATH', os.defpath).split(os.pathsep),
                   str(WORKSPACE_ROOT / '.local' / 'bin')]))})


def workspace_path(path: str) -> Path:
    if not isinstance(path, str) or not path or len(path) > 300:
        raise ToolInputError("path is required")
    root = WORKSPACE_ROOT.resolve()
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root) or candidate == root:
        raise ToolInputError("path is outside workspace")
    return candidate


def _public_url(value: str, https_only: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 2000:
        raise ToolInputError("valid URL is required")
    parsed = urlsplit(value)
    schemes = {"https"} if https_only else {"http", "https"}
    if parsed.scheme not in schemes or not parsed.hostname or parsed.username or parsed.password:
        raise ToolInputError("public HTTPS URL is required" if https_only else "public HTTP(S) URL is required")
    return value


def _run_command(command: list[str],
                 timeout: float = RUNTIME["timeouts"]["tool_command_seconds"],
                 *, save_output: bool = False) -> str:
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace", newline="") as output:
        timed_out = False
        try:
            options = {"cwd": WORKSPACE_ROOT, "env": safe_tool_env(),
                       "stdout": output, "stderr": subprocess.STDOUT,
                       "timeout": timeout, "check": False}
            if os.geteuid() == 0:
                options.update(user=10001, group=10001)
            completed = subprocess.run(command, **options)
        except subprocess.TimeoutExpired:
            timed_out = True
        output.seek(0)
        snippet = output.read(MAX_TOOL_OUTPUT + 1)
        truncated = len(snippet) > MAX_TOOL_OUTPUT
        snippet = snippet[:MAX_TOOL_OUTPUT]
        if truncated:
            snippet += "\n[output truncated]"
        if save_output:
            output.seek(0)
            path = _file_action('save_output', {}, input_file=output).strip()
            snippet += (f"\nFull output: {path}\nRead with read_tool_output path={path} "
                        f"offset={MAX_TOOL_OUTPUT if truncated else 0} limit={MAX_TOOL_OUTPUT}.")
    if timed_out:
        raise ToolExecutionError(f"command timed out; partial output:\n{snippet}")
    return f"exit_code={completed.returncode}\n{snippet}"


def _file_action(action: str, arguments: dict, timeout_seconds: float | None = None,
                 *, input_file=None) -> str:
    options = {"capture_output": True,
               "timeout": min(RUNTIME["timeouts"]["file_action_seconds"], timeout_seconds)
               if timeout_seconds is not None else RUNTIME["timeouts"]["file_action_seconds"],
               "check": False,
               "env": safe_tool_env() | {"TOOL_WORKSPACE_ROOT": str(WORKSPACE_ROOT)}}
    if input_file is None:
        options['input'] = json.dumps(arguments, ensure_ascii=False).encode('utf-8')
    else:
        options['stdin'] = input_file
    if os.geteuid() == 0:
        options.update(user=10001, group=10001)
    completed = subprocess.run(
        [sys.executable, "-I", str(Path(__file__).parent / "worker" / "file_worker.py"),
         action], **options)
    if completed.returncode != 0:
        raise ToolExecutionError(f"{action} failed with exit code {completed.returncode}; "
                                 "check the path, file permissions and workspace state before retrying")
    return completed.stdout.decode('utf-8')


def preserve_tool_output(output: str) -> str:
    """Keep large MCP results in the sandbox instead of losing their tail."""
    if len(output) <= MAX_TOOL_OUTPUT:
        return output
    path = f".tool-results/{uuid.uuid4().hex}.txt"
    _file_action('write', {'path': path, 'content': output})
    return (output[:MAX_TOOL_OUTPUT] + f"\n[preview truncated; total_chars={len(output)}]"
            f"\nFull output: {path}\nRead with read_tool_output path={path} offset={MAX_TOOL_OUTPUT} limit={MAX_TOOL_OUTPUT}.")


def _evaluate(node):
    if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
        value = node.value
    elif isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        value = _BINARY[type(node.op)](_evaluate(node.left), _evaluate(node.right))
    elif isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        value = _UNARY[type(node.op)](_evaluate(node.operand))
    else:
        raise ToolInputError("unsupported expression")
    if not -1e12 <= value <= 1e12:
        raise ToolInputError("result is out of range")
    return value


def execute_tool(name: str, arguments: dict, allowed: list[str],
                 *, timeout_seconds: float | None = None) -> str:
    if name not in allowed or name not in TOOL_SCHEMAS:
        raise ValueError(f"tool is not allowed: {name}")
    if not isinstance(arguments, dict):
        raise ToolInputError("tool arguments must be an object")
    if name == "bash":
        if set(arguments) != {"command"} or not isinstance(arguments["command"], str) \
                or len(arguments["command"]) > 4000:
            raise ToolInputError("bash requires a command up to 4000 characters")
        timeout = RUNTIME["timeouts"]["tool_command_seconds"]
        return _run_command(["bash", "--noprofile", "--norc", "-c", arguments["command"]],
                            timeout=min(timeout, timeout_seconds) if timeout_seconds is not None else timeout,
                            save_output=True)
    if name in {"read_file", "read_tool_output"}:
        if name == 'read_tool_output' and (not isinstance(arguments.get('path'), str)
                or not re.fullmatch(r'\.tool-results/[a-f0-9]{32}\.txt', arguments['path'])):
            raise ToolInputError('read_tool_output requires a saved .tool-results output path')
        if 'path' not in arguments or set(arguments) - {'path', 'offset', 'limit'}:
            raise ToolInputError("read_file requires path and optional offset/limit")
        if type(arguments.get('offset', 0)) is not int or arguments.get('offset', 0) < 0:
            raise ToolInputError("read_file offset must be a non-negative integer")
        limit = arguments.get('limit', MAX_TOOL_OUTPUT)
        if type(limit) is not int or not 1 <= limit <= MAX_TOOL_OUTPUT:
            raise ToolInputError(f"read_file limit must be between 1 and {MAX_TOOL_OUTPUT}")
        # Private artifacts belong to UID 10001; inspect them in the file worker.
        if name == 'read_file':
            workspace_path(arguments["path"])
        return _file_action('read_tool_output' if name == 'read_tool_output' else 'read',
                            arguments, timeout_seconds)
    if name == "write_file":
        if set(arguments) != {"path", "content"} or not isinstance(arguments["content"], str) \
                or len(arguments["content"].encode("utf-8")) > RUNTIME["limits"]["write_file_bytes"]:
            raise ToolInputError(f"write_file requires path and content up to {RUNTIME['limits']['write_file_bytes']} bytes")
        workspace_path(arguments["path"])
        return _file_action("write", arguments, timeout_seconds)
    if name == "git_clone":
        if not {'url', 'directory'} <= set(arguments) or set(arguments) - {'url', 'directory', 'commit_sha'}:
            raise ToolInputError("git_clone requires url, directory and optional commit_sha")
        commit = arguments.get('commit_sha')
        if 'commit_sha' in arguments:
            if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-fA-F]{40}', commit):
                raise ToolInputError('commit_sha must be a full 40-character hexadecimal commit SHA')
            commit = commit.lower()
        url = _public_url(arguments["url"], https_only=True)
        directory = arguments["directory"]
        if not isinstance(directory, str) or not re.fullmatch(CLONE_DIRECTORY_PATTERN, directory):
            raise ToolInputError(
                "directory must be a simple name inside the workspace, e.g. popot-agents "
                "(1-80 letters, digits, dots, underscores or hyphens; start with a letter or digit). "
                "Do not pass an absolute path or slashes.")
        destination = workspace_path(directory)
        if destination.exists():
            raise ToolInputError("clone destination already exists; use the existing checkout "
                                 "or choose a different directory name")
        budget = RUNTIME["timeouts"]["git_clone_seconds"]
        deadline = time.monotonic() + (min(budget, timeout_seconds) if timeout_seconds is not None else budget)
        commands = [["git", "clone", "--depth", "1", *(['--no-checkout'] if commit else []),
                     "--", url, str(destination)]]
        if commit:
            commands.extend([
                ['git', '-C', str(destination), 'fetch', '--depth', '1', 'origin', commit],
                ['git', '-C', str(destination), 'checkout', '--detach', commit],
                ['git', '-C', str(destination), 'rev-parse', 'HEAD'],
            ])
        for command in commands:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ToolExecutionError('git_clone timed out; checkout is unverified')
            try:
                result = _run_command(command, timeout=remaining)
            except RuntimeError as exc:
                raise ToolExecutionError('git_clone failed; checkout is unverified: ' + str(exc)) from exc
            if not result.startswith('exit_code=0\n'):
                raise ToolExecutionError('git_clone failed during ' + command[1 if command[1] != '-C' else 3]
                                         + '; checkout is unverified. Inspect the destination before retrying.\n'
                                         + result)
        if commit and result.split('\n', 1)[1].strip() != commit:
            raise ToolExecutionError('git_clone HEAD does not match commit_sha; checkout is unverified')
        return f"cloned to {destination}\n" + (f'verified_commit={commit}\n' if commit else '') + result
    if name == "download_file":
        if set(arguments) != {"url", "path"}:
            raise ToolInputError("download_file requires url and path")
        url = _public_url(arguments["url"])
        workspace_path(arguments["path"])
        return _file_action("download", {"url": url, "path": arguments["path"]}, timeout_seconds)
    if name == "utc_time":
        if arguments:
            raise ToolInputError("utc_time takes no arguments")
        return datetime.now(timezone.utc).isoformat()
    if set(arguments) != {"expression"} or not isinstance(arguments["expression"], str):
        raise ToolInputError("calculate requires expression")
    expression = arguments["expression"]
    if len(expression) > 100:
        raise ToolInputError("expression is too long")
    try:
        value = _evaluate(ast.parse(expression, mode="eval").body)
    except (SyntaxError, ZeroDivisionError, OverflowError, RecursionError) as exc:
        raise ToolInputError("invalid arithmetic expression") from exc
    return str(value)

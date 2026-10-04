"""HTTP orchestrator for tasks and resumable Docker chat workers."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlsplit

from popot_agents.tools import TOOL_SCHEMAS
from popot_agents.role_env import selected_role_env, validate_role_env_names
from popot_agents.runtime_config import RUNTIME, validate_model_parameters
from popot_agents.skills import load_skill_instructions
from .session_store import SessionStore


MAX_REQUEST_BYTES = RUNTIME["limits"]["request_bytes"]
MAX_TASK_LENGTH = RUNTIME["limits"]["message_chars"]
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CHAT_WORKER_MODULE = "popot_agents.worker.session_worker"
LOG_SECRET_FIELDS = {"api_key", "apikey", "authorization", "cookie", "password",
                     "secret", "token", "access_token", "refresh_token"}


def _is_secret_field(key) -> bool:
    if not isinstance(key, str):
        return False
    name = key.lower()
    return name in LOG_SECRET_FIELDS or name.endswith(
        ("_api_key", "_password", "_secret", "_token"))


def _log_body(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if _is_secret_field(key) else _log_body(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_log_body(item) for item in value]
    return value


class AgentRunError(Exception):
    """The container did not return a valid agent answer."""


class AgentConfigurationError(Exception):
    """A selected agent profile needs operator configuration."""


class DockerAgentRunner:
    def __init__(
        self,
        image: str = "popot-agent-worker:local",
        timeout_seconds: int = RUNTIME["worker"]["default_timeout_seconds"],
        network: str = "none",
        env_names: list[str] | None = None,
        command: list[str] | None = None,
        environment: dict[str, str] | None = None,
        model_env: str | None = None,
        env_aliases: dict[str, str] | None = None,
        session_mode: str = "cli",
        model_parameters: dict | None = None,
        resources: dict | None = None,
    ) -> None:
        if network not in {"none", "bridge"}:
            raise ValueError("network must be 'none' or 'bridge'")
        if session_mode not in {"cli", "http"}:
            raise ValueError("session_mode must be 'cli' or 'http'")
        for name in env_names or []:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(f"invalid environment variable name: {name}")
        if model_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", model_env):
            raise ValueError("invalid model environment variable name")
        for target, source in (env_aliases or {}).items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", target) or source not in (env_names or []):
                raise ValueError("environment aliases must use forwarded variable names")
        model_parameters = validate_model_parameters(
            {} if model_parameters is None else model_parameters, "model_parameters")
        resource_fields = {"memory", "cpus", "pids_limit", "tmpfs_mb", "workspace_tmpfs_mb"}
        if resources is not None and (not isinstance(resources, dict)
                                      or set(resources) - resource_fields):
            raise ValueError("resources has unknown fields")
        for key, value in (resources or {}).items():
            if key == "memory":
                valid = isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*[kmg]", value)
            elif key == "cpus":
                valid = type(value) in (int, float) and value > 0
            else:
                valid = type(value) is int and value > 0
            if not valid:
                raise ValueError(f"resources.{key} is invalid")
        self.image = image
        self.timeout_seconds = timeout_seconds
        self.network = network
        self.env_names = env_names or []
        self.command = command
        self.environment = environment or {}
        self.model_env = model_env
        self.env_aliases = env_aliases or {}
        self.session_mode = session_mode
        self.model_parameters = model_parameters
        self.resources = resources or {}

    @staticmethod
    def workspace_path(container_name: str, role_config: dict | None,
                       create: bool = False) -> Path | None:
        permissions = (role_config or {}).get("permissions", {})
        if permissions.get("workspace") != "persistent":
            return None
        root = Path(os.getenv("AGENT_WORKSPACE_DIR", str(PROJECT_ROOT / ".workspaces"))).resolve()
        path = root / container_name
        if create:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(root, 0o700)
            path.mkdir(mode=0o770, exist_ok=True)
            if os.geteuid() == 0:
                os.chown(path, -1, 10001)
            os.chmod(path, 0o770)
        return path

    @staticmethod
    def workspace_host_path(container_name: str, role_config: dict | None) -> Path | None:
        if (role_config or {}).get("permissions", {}).get("workspace") != "persistent":
            return None
        host_root = os.getenv("AGENT_WORKSPACE_HOST_DIR")
        if host_root:
            root = Path(host_root)
            if not root.is_absolute():
                raise ValueError("AGENT_WORKSPACE_HOST_DIR must be absolute")
            return root / container_name
        return DockerAgentRunner.workspace_path(container_name, role_config)

    def _docker_command(self, container_name: str, detached: bool,
                        role_config: dict | None = None) -> list[str]:
        role_env_names = [name for name in (role_config or {}).get("env_names", [])
                          if name in RUNTIME["role_env_names"]]
        try:
            selected_role_env(role_env_names)
        except ValueError as exc:
            raise AgentConfigurationError(str(exc)) from exc
        for name in self.env_names:
            if not os.getenv(name):
                raise AgentConfigurationError(f"required environment variable is missing: {name}")
        if self.model_env and not os.getenv(self.model_env):
            raise AgentConfigurationError(f"required environment variable is missing: {self.model_env}")
        workspace = self.workspace_path(container_name, role_config, create=True)
        host_workspace = self.workspace_host_path(container_name, role_config)
        configured_tools = set((role_config or {}).get("tools", []))
        resources = {**RUNTIME["worker"], **self.resources}
        isolate_tools = bool(configured_tools & {"bash", "git_clone"}
                             or (role_config or {}).get("mcpServers"))
        command = [
            "docker", "run", "--rm", "--detach" if detached else "--interactive", "--init",
            "--name", container_name,
            "--network", self.network, "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", str(resources["pids_limit"]),
            "--memory", resources["memory"], "--cpus", str(resources["cpus"]),
            "--tmpfs", f"/tmp:rw,uid=10001,gid=10001,size={resources['tmpfs_mb']}m",
            "--env", "HOME=/workspace",
            "--env", "XDG_CACHE_HOME=/workspace/.cache",
            "--env", "XDG_CONFIG_HOME=/workspace/.config",
        ]
        if workspace:
            command.extend(["--mount", f"type=bind,src={host_workspace},dst=/workspace"])
        else:
            command.extend(["--tmpfs", f"/workspace:rw,uid=10001,gid=10001,size={resources['workspace_tmpfs_mb']}m"])
        if isolate_tools:
            command.extend(["--user", "0:10001", "--cap-add", "SETUID",
                            "--cap-add", "SETGID", "--tmpfs",
                            "/run:rw,uid=0,gid=0,mode=0700,size=1m"])
        for name in self.env_names:
            command.extend(["--env", name])
        for name in role_env_names:
            if name not in self.env_names:
                command.extend(["--env", name])
        environment = dict(self.environment)
        if self.command is not None:
            environment["HARNESS_COMMAND_JSON"] = json.dumps(self.command)
        if self.env_aliases:
            environment["HARNESS_ENV_ALIASES_JSON"] = json.dumps(self.env_aliases)
        if self.model_env:
            environment["HARNESS_MODEL"] = os.environ[self.model_env]
        turn_timeout = (role_config or {}).get("timeout_seconds", self.timeout_seconds)
        environment["HARNESS_TURN_TIMEOUT_SECONDS"] = str(max(0.1, turn_timeout - 5))
        if self.model_parameters:
            environment["HARNESS_MODEL_PARAMETERS_JSON"] = json.dumps(self.model_parameters)
        environment["HARNESS_SESSION_MODE"] = self.session_mode
        if isolate_tools:
            environment["HARNESS_SOCKET_PATH"] = "/run/chat.sock"
        if role_config is not None:
            environment["HARNESS_ROLE_JSON"] = json.dumps(role_config, ensure_ascii=False)
            environment["HARNESS_TOOL_ENV_NAMES_JSON"] = json.dumps(role_env_names)
        for name, value in environment.items():
            command.extend(["--env", f"{name}={value}"])
        return command

    def __call__(self, task: str, role_config: dict | None = None) -> dict[str, str]:
        container_name = f"popot-agent-{uuid.uuid4().hex[:12]}"
        command = self._docker_command(container_name, detached=False, role_config=role_config)
        command.append(self.image)
        timeout_seconds = (role_config or {}).get("timeout_seconds", self.timeout_seconds)
        model_env = self.model_env or "none"
        started_at = time.monotonic()
        print(f"worker {container_name} started model_env={model_env} timeout={timeout_seconds}s",
              file=sys.stderr, flush=True)
        try:
            completed = subprocess.run(
                command,
                input=json.dumps({"task": task}),
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            print(f"worker {container_name} timed out after {time.monotonic() - started_at:.1f}s",
                  file=sys.stderr, flush=True)
            try:
                subprocess.run(
                    ["docker", "rm", "-f", container_name],
                    capture_output=True, timeout=RUNTIME["timeouts"]["docker_remove_seconds"], check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
            raise
        if completed.returncode != 0:
            print(f"worker {container_name} failed exit={completed.returncode} "
                  f"after {time.monotonic() - started_at:.1f}s", file=sys.stderr, flush=True)
            detail = completed.stderr.strip().splitlines()
            raise AgentRunError(detail[-1] if detail else "agent container failed")
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise AgentRunError("agent container returned invalid JSON") from exc
        if not isinstance(result, dict) or not isinstance(result.get("answer"), str):
            raise AgentRunError("agent container returned no answer")
        print(f"worker {container_name} completed after {time.monotonic() - started_at:.1f}s",
              file=sys.stderr, flush=True)
        response = {"answer": result["answer"]}
        workspace = self.workspace_host_path(container_name, role_config)
        if workspace:
            response["workspace"] = str(workspace)
        return response

    def start_chat(self, chat_id: str, role_config: dict | None = None) -> DockerChat:
        container_name = f"popot-chat-{chat_id}"
        command = self._docker_command(container_name, detached=True, role_config=role_config)
        command.extend(["--label", f"popot.chat_id={chat_id}", self.image,
                        "python", "-u", "-m", CHAT_WORKER_MODULE, "serve"])
        started = subprocess.run(command, text=True, capture_output=True,
                                 timeout=RUNTIME["timeouts"]["docker_start_seconds"], check=False)
        if started.returncode != 0:
            existing = subprocess.run(
                ["docker", "inspect", "--format",
                 '{{ index .Config.Labels "popot.chat_id" }}', container_name],
                text=True, capture_output=True, timeout=RUNTIME["timeouts"]["docker_inspect_seconds"], check=False,
            )
            if existing.returncode != 0 or existing.stdout.strip() != chat_id:
                detail = started.stderr.strip().splitlines()
                raise AgentRunError(detail[-1] if detail else "chat container failed to start")
        chat = DockerChat(container_name,
                          (role_config or {}).get("timeout_seconds", self.timeout_seconds),
                          self.workspace_host_path(container_name, role_config))
        for _ in range(RUNTIME["worker"]["startup_probe_attempts"]):
            ready = subprocess.run(
                ["docker", "exec", container_name, "python", "-m", CHAT_WORKER_MODULE, "ping"],
                text=True, capture_output=True, timeout=RUNTIME["timeouts"]["docker_probe_seconds"], check=False,
            )
            if ready.returncode == 0:
                try:
                    if json.loads(ready.stdout).get("status") == "ok":
                        print(f"chat worker {container_name} ready", file=sys.stderr, flush=True)
                        return chat
                except json.JSONDecodeError:
                    pass
            time.sleep(RUNTIME["worker"]["startup_probe_interval_seconds"])
        chat.close()
        raise AgentRunError("chat worker did not become ready")


class DockerChat:
    def __init__(self, container_name: str, timeout_seconds: int,
                 workspace_path: Path | None = None) -> None:
        self.container_name = container_name
        self.timeout_seconds = timeout_seconds
        self.workspace_path = str(workspace_path) if workspace_path else None
        self.closed = False
        self._last_log_lines: list[str] = []

    def _capture_logs(self) -> None:
        log_dir = os.getenv("AGENT_WORKER_LOG_DIR")
        match = re.fullmatch(r"popot-chat-([0-9a-f]{16})", self.container_name)
        if not log_dir or match is None:
            return
        try:
            completed = subprocess.run(
                ["docker", "logs", "--tail", str(RUNTIME["logging"]["worker_log_tail_lines"]),
                 self.container_name],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=RUNTIME["timeouts"]["docker_inspect_seconds"], check=False,
            )
            if completed.returncode != 0:
                return
            lines = completed.stdout.splitlines(keepends=True)
            overlap = 0
            for count in range(min(len(self._last_log_lines), len(lines)), 0, -1):
                if self._last_log_lines[-count:] == lines[:count]:
                    overlap = count
                    break
            self._last_log_lines = lines
            addition = "".join(lines[overlap:]).encode("utf-8")
            if not addition:
                return
            directory = Path(log_dir)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = directory / f"{match.group(1)}.log"
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o600)
            with os.fdopen(descriptor, "r+b") as file:
                existing = file.read()
                content = (existing + addition)[-RUNTIME["logging"]["worker_log_max_bytes"]:]
                content = content.decode("utf-8", errors="ignore").encode("utf-8")
                file.seek(0)
                file.write(content)
                file.truncate()
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"worker log capture failed for {self.container_name}: {type(exc).__name__}",
                  file=sys.stderr, flush=True)

    def is_alive(self) -> bool:
        if self.closed:
            return False
        try:
            completed = subprocess.run(
                ["docker", "exec", self.container_name,
                 "python", "-m", CHAT_WORKER_MODULE, "ping"],
                text=True, capture_output=True, timeout=RUNTIME["timeouts"]["docker_probe_seconds"], check=False,
            )
            return completed.returncode == 0 and json.loads(completed.stdout).get("status") == "ok"
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            return False

    def restore(self, messages: list[dict[str, str]]) -> None:
        completed = subprocess.run(
            ["docker", "exec", "--interactive", self.container_name,
             "python", "-m", CHAT_WORKER_MODULE, "restore"],
            input=json.dumps({"messages": messages}, ensure_ascii=False),
            text=True, capture_output=True, timeout=RUNTIME["timeouts"]["docker_restore_seconds"], check=False,
        )
        if completed.returncode != 0:
            raise AgentRunError("chat worker could not restore history")
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise AgentRunError("chat worker returned invalid restore response") from exc
        if result.get("status") != "ok":
            raise AgentRunError(result.get("error", "chat worker could not restore history"))

    def send(self, message: str) -> dict[str, str]:
        try:
            completed = subprocess.run(
                ["docker", "exec", "--interactive", self.container_name,
                 "python", "-m", CHAT_WORKER_MODULE, "message"],
                input=json.dumps({"message": message}, ensure_ascii=False),
                text=True, capture_output=True, timeout=self.timeout_seconds, check=False,
            )
        finally:
            self._capture_logs()
        if completed.returncode != 0:
            raise AgentRunError("chat worker is unavailable")
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise AgentRunError("chat worker returned invalid JSON") from exc
        if isinstance(result, dict) and isinstance(result.get("error"), str):
            raise AgentRunError(result["error"])
        if not isinstance(result, dict) or not isinstance(result.get("answer"), str):
            raise AgentRunError("chat worker returned no answer")
        return {"answer": result["answer"]}

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._capture_logs()
            try:
                subprocess.run(["docker", "rm", "-f", self.container_name],
                               text=True, capture_output=True,
                               timeout=RUNTIME["timeouts"]["docker_remove_seconds"], check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass


def remove_worker_log(session_id: str) -> None:
    log_dir = os.getenv("AGENT_WORKER_LOG_DIR")
    if not log_dir or re.fullmatch(r"[0-9a-f]{16}", session_id) is None:
        return
    try:
        (Path(log_dir) / f"{session_id}.log").unlink(missing_ok=True)
    except OSError as exc:
        print(f"worker log cleanup failed for {session_id}: {type(exc).__name__}",
              file=sys.stderr, flush=True)


def load_agents(path: str) -> dict[str, DockerAgentRunner]:
    with open(path, encoding="utf-8") as file:
        profiles = json.load(file)
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError("agents file must contain at least one profile")
    agents = {}
    for name, profile in profiles.items():
        if not isinstance(profile, dict):
            raise ValueError(f"invalid agent profile: {name}")
        agents[name] = DockerAgentRunner(**profile)
    return agents


def load_roles(path: str | Path, agents: Mapping[str, DockerAgentRunner]) -> dict[str, dict]:
    with open(path, encoding="utf-8") as file:
        roles = json.load(file)
    if not isinstance(roles, dict):
        raise ValueError("roles file must be a JSON object")
    for name, config in roles.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError(f"invalid role name: {name}")
        required = {"agent", "instructions", "tools"}
        optional = {"permissions", "mcpServers", "timeout_seconds", "max_tool_rounds",
                    "ttl_seconds", "allowed_roles", "cron", "env_names", "skills", "description"}
        if not isinstance(config, dict) or not required.issubset(config) \
                or set(config) - required - optional:
            raise ValueError(f"role {name} must define agent, instructions and tools")
        agent = config["agent"]
        if not isinstance(agent, str) or agent not in agents:
            raise ValueError(f"role {name} uses an unknown agent")
        if not isinstance(config["instructions"], str) or len(config["instructions"]) > 4000:
            raise ValueError(f"role {name} has invalid instructions")
        if "description" in config and (not isinstance(config["description"], str)
                or not config["description"].strip() or len(config["description"]) > 1000):
            raise ValueError(f"role {name} has invalid description")
        try:
            load_skill_instructions(config.get("skills", []))
        except ValueError as exc:
            raise ValueError(f"role {name} has invalid skills: {exc}") from exc
        allowed_roles = config.get("allowed_roles", [])
        if not isinstance(allowed_roles, list) or any(
                not isinstance(target, str) or target not in roles or target == name
                for target in allowed_roles):
            raise ValueError(f"role {name} has invalid allowed_roles: use existing other roles")
        if len(allowed_roles) != len(set(allowed_roles)):
            raise ValueError(f"role {name} has duplicate allowed_roles")
        if "env_names" in config:
            try:
                selected = validate_role_env_names(config["env_names"])
            except ValueError as exc:
                raise ValueError(f"role {name} has invalid env_names") from exc
            if not set(selected).issubset(RUNTIME["role_env_names"]):
                raise ValueError(f"role {name} env_names must be allowed by runtime config")
        if "cron" in config:
            from .cron import validate_cron
            schedules = config["cron"]
            if not isinstance(schedules, list) or not schedules:
                raise ValueError(f"role {name} has invalid cron schedules")
            for schedule in schedules:
                if not isinstance(schedule, dict) or set(schedule) != {"schedule", "task"} \
                        or not isinstance(schedule["task"], str) \
                        or not schedule["task"].strip() \
                        or len(schedule["task"]) > RUNTIME["limits"]["message_chars"]:
                    raise ValueError(f"role {name} has invalid cron task")
                try:
                    validate_cron(schedule["schedule"])
                except ValueError as exc:
                    raise ValueError(f"role {name} has invalid cron expression") from exc
        tools = config["tools"]
        if not isinstance(tools, list) or any(not isinstance(tool, str) for tool in tools):
            raise ValueError(f"role {name} has invalid tools")
        if len(tools) != len(set(tools)):
            raise ValueError(f"role {name} has duplicate tools")
        if any(tool not in TOOL_SCHEMAS for tool in tools):
            raise ValueError(f"role {name} has an unknown tool")
        if tools and agents[agent].session_mode != "http":
            raise ValueError(f"role {name} needs an HTTP agent for built-in tools")
        permissions = config.get("permissions", {})
        if not isinstance(permissions, dict) or set(permissions) - {"workspace", "shell", "internet"}:
            raise ValueError(f"role {name} has invalid permissions")
        if not isinstance(permissions.get("workspace", "temporary"), str) \
                or permissions.get("workspace", "temporary") not in {"temporary", "persistent"} \
                or not all(isinstance(permissions.get(key, False), bool)
                           for key in ("shell", "internet")):
            raise ValueError(f"role {name} has invalid permissions")
        if "bash" in tools and not permissions.get("shell"):
            raise ValueError(f"role {name} must grant shell permission for bash")
        if {"git_clone", "download_file"} & set(tools) and not permissions.get("internet"):
            raise ValueError(f"role {name} must grant internet permission")
        if permissions.get("shell") and not permissions.get("internet"):
            raise ValueError(f"role {name} must grant internet permission for shell")
        if {"bash", "read_file", "write_file", "git_clone", "download_file"} & set(tools) \
                and permissions.get("workspace") != "persistent":
            raise ValueError(f"role {name} needs a persistent workspace")
        if permissions.get("internet") and agents[agent].network != "bridge":
            raise ValueError(f"role {name} needs a networked agent profile")
        timeout = config.get("timeout_seconds", RUNTIME["worker"]["default_timeout_seconds"])
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 300:
            raise ValueError(f"role {name} has invalid timeout_seconds")
        ttl = config.get("ttl_seconds", RUNTIME["sessions"]["default_idle_seconds"])
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 0:
            raise ValueError(f"role {name} has invalid ttl_seconds")
        rounds = config.get("max_tool_rounds", RUNTIME["model"]["default_max_tool_rounds"])
        if not isinstance(rounds, int) or isinstance(rounds, bool) or not 1 <= rounds <= 20:
            raise ValueError(f"role {name} has invalid max_tool_rounds")
        mcp_servers = config.get("mcpServers", {})
        if not isinstance(mcp_servers, dict) or (mcp_servers and agents[agent].session_mode != "http"):
            raise ValueError(f"role {name} needs an HTTP agent for MCP servers")
        if mcp_servers and not permissions.get("internet"):
            raise ValueError(f"role {name} must grant internet permission for MCP servers")
        for alias, server in mcp_servers.items():
            if not isinstance(alias, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", alias) \
                    or not isinstance(server, dict) or set(server) not in (
                        {"command", "tools"}, {"url", "bearer_token_env", "tools"}):
                raise ValueError(f"role {name} has invalid MCP server")
            offered = server["tools"]
            if "url" in server:
                url = server["url"]
                if not isinstance(url, str):
                    raise ValueError(f"role {name} has invalid MCP URL")
                parsed = urlsplit(url)
                if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                        or parsed.password is not None or parsed.query or parsed.fragment
                        or any(char.isspace() for char in url)):
                    raise ValueError(f"role {name} needs a credential-free HTTPS MCP URL")
                token_name = server["bearer_token_env"]
                if not isinstance(token_name, str) or token_name not in config.get("env_names", []):
                    raise ValueError(f"role {name} MCP token must be selected in env_names")
            else:
                command = server["command"]
                if not isinstance(command, list) or not command or not all(
                        isinstance(part, str) and part for part in command):
                    raise ValueError(f"role {name} has invalid MCP command")
            if not isinstance(offered, list) or not offered or not all(
                    isinstance(tool, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", tool)
                    for tool in offered) or len(offered) != len(set(offered)):
                raise ValueError(f"role {name} has invalid MCP tools")
    return roles


def load_dotenv(path: str | Path) -> None:
    """Load simple KEY=value settings; exported process variables take priority."""
    file = Path(path)
    if not file.exists():
        return
    for line_number, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"invalid .env entry on line {line_number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if value:
            os.environ.setdefault(name, value)


@dataclass
class ChatRecord:
    agent: str
    worker: DockerChat
    messages: list[dict[str, str]]
    role: str | None = None
    role_config: dict | None = None
    last_used: float = field(default_factory=time.monotonic)


def session_has_active_owner(server, session_id: str) -> bool:
    return any(owner is not None and owner.is_active(session_id)
               for owner in (getattr(server, "delegation", None),
                             getattr(server, "cron", None)))


class ChatServer(ThreadingHTTPServer):
    daemon_threads = False

    def __init__(self, address, handler, session_dir: str | Path, idle_seconds: int,
                 store=None):
        self.store = store if store is not None else SessionStore(session_dir)
        self.live: dict[str, ChatRecord] = {}
        self.live_lock = threading.RLock()
        self.session_locks = [threading.RLock() for _ in range(64)]
        self.task_slots = threading.BoundedSemaphore(RUNTIME["worker"]["max_concurrent_tasks"])
        self.pending_expired: set[str] = set()
        self.idle_seconds = idle_seconds
        self.last_prune = 0.0
        super().__init__(address, handler)

    def session_lock(self, session_id: str):
        return self.session_locks[hash(session_id) % len(self.session_locks)]

    def service_actions(self) -> None:
        now = time.monotonic()
        if now - self.last_prune >= RUNTIME["sessions"]["prune_interval_seconds"]:
            self.last_prune = now
            try:
                expired = self.store.expired_ids()
            except OSError:
                expired = set()
            self.pending_expired.update(expired)
        for session_id in tuple(self.pending_expired):
            lock = self.session_lock(session_id)
            if not lock.acquire(blocking=False):
                continue
            try:
                if session_has_active_owner(self, session_id):
                    continue
                try:
                    deleted = self.store.delete_expired(session_id)
                except OSError:
                    continue
                self.pending_expired.discard(session_id)
                if not deleted:
                    continue
                with self.live_lock:
                    record = self.live.pop(session_id, None)
                if record is not None:
                    record.worker.close()
                remove_worker_log(session_id)
            finally:
                lock.release()
        with self.live_lock:
            session_ids = list(self.live)
        for session_id in session_ids:
            lock = self.session_lock(session_id)
            if not lock.acquire(blocking=False):
                continue
            try:
                with self.live_lock:
                    record = self.live.get(session_id)
                if record is None:
                    continue
                ttl = (record.role_config or {}).get("ttl_seconds", self.idle_seconds)
                if ttl > 0 and now - record.last_used > ttl:
                    with self.live_lock:
                        self.live.pop(session_id, None)
                    record.worker.close()
            finally:
                lock.release()

    def server_close(self) -> None:
        super().server_close()
        with self.live_lock:
            records = list(self.live.values())
            self.live.clear()
        for record in records:
            record.worker.close()


def create_server(
    agents: Mapping[str, Callable[[str], dict[str, str]]],
    host: str = "127.0.0.1",
    port: int = 8000,
    session_dir: str | Path | None = None,
    idle_seconds: int = RUNTIME["sessions"]["default_idle_seconds"],
    roles: Mapping[str, dict] | None = None,
    store=None,
) -> ChatServer:
    roles = roles or {}
    class Handler(BaseHTTPRequestHandler):
        def _log_request(self, body) -> None:
            self._request_id = uuid.uuid4().hex[:12]
            print(json.dumps({
                "event": "request", "request_id": self._request_id,
                "client_ip": self.client_address[0],
                "client_port": self.client_address[1],
                "user_agent": self.headers.get("User-Agent"),
                "method": self.command, "path": self.path, "body": _log_body(body),
            }, ensure_ascii=False), file=sys.stderr, flush=True)

        def _workspace(self, saved: dict, live: ChatRecord | None = None) -> str | None:
            if live is not None:
                return getattr(live.worker, "workspace_path", None)
            runner = agents.get(saved["agent"])
            if runner is None or not hasattr(runner, "workspace_path"):
                return None
            container_name = f"popot-chat-{saved['sessionId']}"
            local_path = runner.workspace_path(container_name, saved.get("roleConfig"))
            if not local_path or not local_path.exists():
                return None
            return str(runner.workspace_host_path(container_name, saved.get("roleConfig")))

        def _reply(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            print(json.dumps({
                "event": "response", "request_id": self._request_id,
                "client_ip": self.client_address[0],
                "method": self.command, "path": self.path,
                "status": status, "body": _log_body(payload),
            }, ensure_ascii=False), file=sys.stderr, flush=True)

        def do_GET(self) -> None:
            self._log_request(None)
            try:
                self._get()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except OSError:
                self._reply(500, {"error": "session storage failed"})

        def _get(self) -> None:
            if self.path == "/healthz":
                self._reply(200, {"status": "ok"})
            elif self.path == "/chats":
                with self.server.live_lock:
                    running = set(self.server.live)
                self._reply(200, {"chats": [
                    {
                        "sessionId": saved["sessionId"],
                        "agent": saved["agent"],
                        "role": saved.get("role"),
                        "createdAt": saved.get("createdAt", saved["updatedAt"]),
                        "expiresAt": (expiry.isoformat() if (expiry := self.server.store.expires_at(saved))
                                      else None),
                        "updatedAt": saved["updatedAt"],
                        "turns": len(saved["messages"]) // 2,
                        "status": "running" if saved["sessionId"] in running else "stopped",
                    }
                    for saved in self.server.store.list_chats()
                ]})
            elif self.path.startswith("/chats/"):
                session_id = self.path.removeprefix("/chats/")
                with self.server.session_lock(session_id):
                    self._get_chat(session_id)
            else:
                self._reply(404, {"error": "not found"})

        def _get_chat(self, session_id: str) -> None:
            saved = self.server.store.get(session_id)
            if saved is None:
                self._reply(404, {"error": "unknown sessionId"})
            else:
                with self.server.live_lock:
                    live = self.server.live.get(session_id)
                if live and not live.worker.is_alive():
                    live.worker.close()
                    with self.server.live_lock:
                        self.server.live.pop(session_id, None)
                    live = None
                self._reply(200, {
                    "sessionId": session_id, "agent": saved["agent"],
                    "role": saved.get("role"),
                    "createdAt": saved.get("createdAt", saved["updatedAt"]),
                    "expiresAt": (expiry.isoformat() if (expiry := self.server.store.expires_at(saved))
                                  else None),
                    "status": "running" if live else "stopped",
                    "container": live.worker.container_name if live else None,
                    "workspace": self._workspace(saved, live),
                    "turns": len(saved["messages"]) // 2,
                })

        def _read_payload(self) -> dict | None:
            if self.headers.get_content_type() != "application/json":
                self._log_request(None)
                self._reply(415, {"error": "Content-Type must be application/json"})
                return None
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if not 0 < length <= MAX_REQUEST_BYTES:
                self._log_request(None)
                self._reply(413, {"error": "invalid request size"})
                return None
            try:
                payload = json.loads(self.rfile.read(length))
            except OSError:
                self._log_request({"read_error": True})
                raise
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._log_request({"invalid_json": True})
                self._reply(400, {"error": "invalid JSON"})
                return None
            self._log_request(payload)
            if not isinstance(payload, dict):
                self._reply(400, {"error": "JSON object is required"})
                return None
            return payload

        def _task(self, payload: dict) -> None:
            task = payload.get("task")
            if not isinstance(task, str) or not task.strip() or len(task) > MAX_TASK_LENGTH:
                self._reply(400, {"error": f"task must be a nonempty string up to {MAX_TASK_LENGTH} characters"})
                return
            role = payload.get("role")
            if role is not None and (not isinstance(role, str) or role not in roles):
                self._reply(404, {"error": "unknown role"})
                return
            role_config = roles.get(role) if role else None
            if not role_config and "agent" not in payload:
                self._reply(400, {"error": "agent or role is required"})
                return
            agent = role_config["agent"] if role_config else payload["agent"]
            if role_config and "agent" in payload and payload["agent"] != agent:
                self._reply(409, {"error": "role uses another agent"})
                return
            if not isinstance(agent, str) or agent not in agents:
                self._reply(404, {"error": "unknown agent"})
                return
            if hasattr(agents[agent], "start_chat"):
                self._message({"agent": agent, "role": role, "message": task.strip()})
                return
            result = agents[agent](task.strip(), role_config) if role_config else agents[agent](task.strip())
            if role_config:
                result = {**result, "role": role, "agent": agent}
            self._reply(200, result)

        def _message(self, payload: dict) -> None:
            session_id = payload.get("sessionId")
            if session_id is None:
                new_session_id = uuid.uuid4().hex[:16]
                with self.server.session_lock(new_session_id):
                    self._message_locked(payload, new_session_id)
            elif isinstance(session_id, str):
                with self.server.session_lock(session_id):
                    self._message_locked(payload)
            else:
                self._message_locked(payload)

        def _message_locked(self, payload: dict, new_session_id: str | None = None) -> None:
            message = payload.get("message")
            if not isinstance(message, str) or not message.strip() or len(message) > MAX_TASK_LENGTH:
                self._reply(400, {"error": f"message must be a nonempty string up to {MAX_TASK_LENGTH} characters"})
                return
            session_id = payload.get("sessionId")
            if session_id is None:
                role = payload.get("role")
                if role is not None and (not isinstance(role, str) or role not in roles):
                    self._reply(404, {"error": "unknown role"})
                    return
                role_config = roles.get(role) if role else None
                if not role_config and "agent" not in payload:
                    self._reply(400, {"error": "agent or role is required"})
                    return
                agent = role_config["agent"] if role_config else payload["agent"]
                if role_config and "agent" in payload and payload["agent"] != agent:
                    self._reply(409, {"error": "role uses another agent"})
                    return
                if not isinstance(agent, str) or agent not in agents:
                    self._reply(404, {"error": "unknown agent"})
                    return
                session_id = new_session_id
                worker = (agents[agent].start_chat(session_id, role_config) if role_config
                          else agents[agent].start_chat(session_id))
                record = ChatRecord(agent, worker, [], role, role_config)
                with self.server.live_lock:
                    self.server.live[session_id] = record
                try:
                    self.server.store.save(session_id, agent, [], role, role_config)
                except OSError:
                    worker.close()
                    with self.server.live_lock:
                        self.server.live.pop(session_id, None)
                    raise
            else:
                if not isinstance(session_id, str):
                    self._reply(400, {"error": "sessionId must be a string"})
                    return
                saved = self.server.store.get(session_id)
                if saved is None:
                    self._reply(404, {"error": "unknown sessionId"})
                    return
                agent = saved["agent"]
                if agent not in agents:
                    self._reply(503, {"error": "saved agent profile is unavailable"})
                    return
                if "agent" in payload and payload["agent"] != agent:
                    self._reply(409, {"error": "sessionId belongs to another agent"})
                    return
                role = saved.get("role")
                role_config = saved.get("roleConfig")
                if "role" in payload and payload["role"] != role:
                    self._reply(409, {"error": "sessionId belongs to another role"})
                    return
                with self.server.live_lock:
                    record = self.server.live.get(session_id)
                if record is not None and not record.worker.is_alive():
                    record.worker.close()
                    with self.server.live_lock:
                        self.server.live.pop(session_id, None)
                    record = None
                if record is None:
                    worker = (agents[agent].start_chat(session_id, role_config) if role_config
                              else agents[agent].start_chat(session_id))
                    try:
                        worker.restore(saved["messages"])
                    except Exception:
                        worker.close()
                        raise
                    record = ChatRecord(agent, worker, saved["messages"], role, role_config)
                    with self.server.live_lock:
                        self.server.live[session_id] = record
            try:
                result = record.worker.send(message.strip())
                history = record.messages + [
                    {"role": "user", "content": message.strip()},
                    {"role": "assistant", "content": result["answer"]},
                ]
                self.server.store.save(session_id, record.agent, history,
                                       record.role, record.role_config)
                record.messages = history
                record.last_used = time.monotonic()
            except AgentRunError:
                if not record.worker.is_alive():
                    record.worker.close()
                    with self.server.live_lock:
                        self.server.live.pop(session_id, None)
                raise
            except (subprocess.TimeoutExpired, OSError):
                record.worker.close()
                with self.server.live_lock:
                    self.server.live.pop(session_id, None)
                raise
            self._reply(200, {
                "sessionId": session_id, "container": record.worker.container_name,
                "answer": result["answer"], "role": record.role, "agent": record.agent,
                "workspace": getattr(record.worker, "workspace_path", None),
            })

        def do_POST(self) -> None:
            if self.path not in {"/tasks", "/messages"}:
                self._log_request(None)
                self._reply(404, {"error": "not found"})
                return
            payload = self._read_payload()
            if payload is None:
                return
            if not self.server.task_slots.acquire(blocking=False):
                self._reply(503, {"error": "orchestrator is busy; retry later"})
                return
            try:
                if self.path == "/tasks":
                    self._task(payload)
                else:
                    self._message(payload)
            except subprocess.TimeoutExpired:
                self._reply(504, {"error": "agent timed out"})
            except FileNotFoundError:
                self._reply(503, {"error": "Docker CLI is not installed"})
            except AgentConfigurationError as exc:
                self._reply(503, {"error": str(exc)})
            except AgentRunError as exc:
                self._reply(502, {"error": str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except OSError:
                self._reply(500, {"error": "session storage failed"})
            finally:
                self.server.task_slots.release()

        def do_DELETE(self) -> None:
            self._log_request(None)
            try:
                self._delete()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except OSError:
                self._reply(500, {"error": "session storage failed"})

        def _delete(self) -> None:
            if not self.path.startswith("/chats/"):
                self._reply(404, {"error": "not found"})
                return
            session_id = self.path.removeprefix("/chats/")
            with self.server.session_lock(session_id):
                self._delete_chat(session_id)

        def _delete_chat(self, session_id: str) -> None:
            if self.server.store.get(session_id) is None:
                self._reply(404, {"error": "unknown sessionId"})
                return
            if session_has_active_owner(self.server, session_id):
                self._reply(409, {"error": "session is owned by an active task"})
                return
            with self.server.live_lock:
                record = self.server.live.pop(session_id, None)
            if record is not None:
                record.worker.close()
            self.server.store.delete(session_id)
            remove_worker_log(session_id)
            self._reply(200, {"status": "closed"})

    directory = session_dir or PROJECT_ROOT / ".sessions"
    return ChatServer((host, port), Handler, directory, idle_seconds, store)


def main() -> None:
    load_dotenv(os.getenv("AGENT_ENV_FILE", str(PROJECT_ROOT / ".env")))
    host = os.getenv("API_HOST", "127.0.0.1")
    port = int(os.getenv("API_PORT", "8000"))
    agents = load_agents(os.getenv("AGENT_PROFILES_FILE", str(PROJECT_ROOT / "config" / "agents.json")))
    roles = load_roles(os.getenv("AGENT_ROLES_FILE", str(PROJECT_ROOT / "config" / "roles.json")), agents)
    store = None
    if os.getenv("AGENT_SESSION_BACKEND") == "postgres":
        from .postgres_session_store import PostgresSessionStore
        store = PostgresSessionStore()
        if os.getenv("AGENT_LEGACY_SESSION_DIR"):
            store.import_legacy(os.environ["AGENT_LEGACY_SESSION_DIR"])
    server = create_server(
        agents, host, port,
        session_dir=os.getenv("AGENT_SESSION_DIR", str(PROJECT_ROOT / ".sessions")),
        idle_seconds=int(os.getenv("AGENT_CHAT_IDLE_SECONDS", str(RUNTIME["sessions"]["default_idle_seconds"]))),
        roles=roles,
        store=store,
    )
    print(f"Listening on http://{host}:{port}", flush=True)
    def stop_on_sigterm(_signum, _frame):
        raise KeyboardInterrupt

    previous_handler = signal.signal(signal.SIGTERM, stop_on_sigterm)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
        server.server_close()


if __name__ == "__main__":
    main()

"""AX Task adapter for the existing HTTP chat API."""

from __future__ import annotations

import base64
from contextlib import nullcontext
import json
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field

from popot_agents.orchestrator.main import AgentConfigurationError, AgentRunError
from popot_agents.role_env import selected_role_env
from popot_agents.runtime_config import RUNTIME
from ..config import AX_CONFIG
from ..worker.remote_client import RESPONSE_PREFIX


class AxAgentRunner:
    session_mode = "http"
    network = "bridge"

    def __init__(self, image: str, model: str, base_url: str,
                 context: str = AX_CONFIG["ax"]["context"],
                 proxy_token: str | None = None) -> None:
        if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image):
            raise ValueError("AX worker image must be pinned by sha256 digest")
        if not context.startswith("kind-") or not re.fullmatch(r"[a-z0-9-]+", context):
            raise ValueError("AX comparison requires a local kind context")
        if not model or not base_url.startswith(("http://", "https://")):
            raise ValueError("AX_LOCAL_MODEL and AX_LOCAL_BASE_URL are required")
        self.image = image
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.context = context
        self.proxy_token = proxy_token
        self.delegation = None

    def _run(self, *args: str, input: str | None = None,
             timeout: int = AX_CONFIG["ax"]["cli_timeout_seconds"],
             allow_failure: bool = False) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                ["ax", f"--context={self.context}",
                 f"--atespace={AX_CONFIG['ax']['atespace']}", *args], input=input,
                text=True, capture_output=True, timeout=timeout, check=False,
            )
        except FileNotFoundError as exc:
            raise AgentConfigurationError("AX CLI is not installed") from exc
        if result.returncode and not allow_failure:
            detail = result.stderr.strip().splitlines()
            raise AgentRunError(detail[-1] if detail else f"AX command failed: {args[0]}")
        return result

    @staticmethod
    def workspace_manifest() -> dict:
        return {
            "apiVersion": "ax.io/v1alpha1", "kind": "Workspace",
            "metadata": {"name": AX_CONFIG["ax"]["workspace_name"],
                         "atespace": AX_CONFIG["ax"]["atespace"]},
            "spec": {},
        }

    def task_manifest(self, chat_id: str, role_config: dict | None = None) -> dict:
        if not re.fullmatch(r"[0-9a-f]{16}", chat_id):
            raise ValueError("invalid session ID")
        role_config = role_config or {}
        env = {
            "HARNESS_BASE_URL": self.base_url,
            "HARNESS_MODEL": self.model,
            "HARNESS_ROLE_JSON": json.dumps(role_config, ensure_ascii=False),
            "HARNESS_SESSION_MODE": "http",
            "HARNESS_SOCKET_PATH": "/workspace/chat.sock",
            "PYTHONPATH": "/app",
            "HOME": "/workspace",
            "XDG_CACHE_HOME": "/workspace/.cache",
            "XDG_CONFIG_HOME": "/workspace/.config",
            "HARNESS_TURN_TIMEOUT_SECONDS": str(role_config.get(
                "timeout_seconds", RUNTIME["worker"]["default_timeout_seconds"])),
        }
        if role_config.get("mcpServers") or set(role_config.get("tools", [])) & {
                "bash", "git_clone", "read_file", "write_file", "download_file"}:
            env["POPOT_TOOLS_UID"] = "10001"
        if self.delegation:
            env.update(self.delegation.worker_environment(chat_id, role_config))
            env["AX_DELEGATION_POLL_SECONDS"] = str(self.delegation.policy["poll_interval_seconds"])
        if self.proxy_token:
            env["HARNESS_API_KEY_ENV"] = "AX_PROXY_TOKEN"
            env["AX_PROXY_TOKEN"] = self.proxy_token
        try:
            names = [name for name in role_config.get("env_names", [])
                     if name in RUNTIME["role_env_names"]]
            env.update(selected_role_env(names))
            env["HARNESS_TOOL_ENV_NAMES_JSON"] = json.dumps(names)
        except ValueError as exc:
            raise AgentConfigurationError(str(exc)) from exc
        return {
            "apiVersion": "ax.io/v1alpha1", "kind": "Task",
            "metadata": {"name": f"popot-chat-{chat_id}",
                         "atespace": AX_CONFIG["ax"]["atespace"]},
            "spec": {
                "image": self.image,
                "command": ["python", "-u", "-m", "ax_local.worker.entrypoint"],
                "env": [{"name": key, "value": value} for key, value in env.items()],
                "resources": AX_CONFIG["ax"]["task_resources"],
                "workspaces": [{"name": AX_CONFIG["ax"]["workspace_name"],
                                "path": "/workspace"}],
                "debug": AX_CONFIG["ax"]["task_debug"],
            },
        }

    def remote(self, task_name: str, payload: dict, timeout: int = 60) -> dict:
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        if len(encoded) <= 48000:
            return self._remote_command(task_name, encoded, timeout=timeout)
        request_id = uuid.uuid4().hex
        for offset in range(0, len(encoded), 48000):
            self._remote_command(task_name, "--chunk", request_id,
                                 encoded[offset:offset + 48000], timeout=timeout)
        return self._remote_command(task_name, "--consume", request_id, timeout=timeout)

    def _remote_command(self, task_name: str, *arguments: str, timeout: int) -> dict:
        result = self._run("ssh", task_name, "--", "python",
                           "/app/ax_local/worker/remote_client.py", *arguments, timeout=timeout)
        for line in reversed(result.stdout.splitlines()):
            if line.startswith(RESPONSE_PREFIX):
                try:
                    answer = json.loads(line[len(RESPONSE_PREFIX):])
                except json.JSONDecodeError as exc:
                    raise AgentRunError("AX worker returned invalid JSON") from exc
                if isinstance(answer, dict):
                    return answer
        raise AgentRunError("AX worker returned no response")

    def task_status(self, task_name: str, timeout: float = 15) -> tuple[str | None, str | None]:
        """Read only AX status fields; never expose Task environment values."""
        result = self._run("describe", "task", task_name, timeout=timeout, allow_failure=True)
        if result.returncode:
            return None, None
        phase = None
        reason = None
        in_conditions = False
        for line in result.stdout.splitlines():
            if line.startswith("Phase:"):
                candidate = line.partition(":")[2].strip()
                if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", candidate):
                    phase = candidate
            elif line == "Conditions:":
                in_conditions = True
            elif in_conditions:
                fields = line.split(maxsplit=3)
                if len(fields) >= 3 and fields[1] in {"True", "False", "Unknown"} \
                        and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", fields[2]):
                    reason = fields[2]
        return phase, reason

    def start_chat(self, chat_id: str, role_config: dict | None = None,
                   *, deadline: float | None = None, cancel_event=None) -> AxChat:
        if self.delegation:
            role_config = self.delegation.prepare_config(chat_id, role_config)
        task_name = f"popot-chat-{chat_id}"
        def remaining(limit):
            if cancel_event is not None and cancel_event.is_set():
                raise TimeoutError("AX startup canceled")
            if deadline is None:
                return limit
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise TimeoutError("AX startup deadline exceeded")
            return min(limit, budget)

        existing = self._run("get", "task", task_name, allow_failure=True,
                             timeout=remaining(AX_CONFIG["ax"]["cli_timeout_seconds"]))
        if existing.returncode and "NotFound" not in existing.stderr:
            raise AgentRunError("AX task lookup failed")
        if not existing.returncode:
            # A previous API process may have left a task with an expired proxy token.
            try:
                self._run("delete", "task", task_name,
                          timeout=remaining(AX_CONFIG["ax"]["cli_timeout_seconds"]))
            except subprocess.TimeoutExpired as exc:
                # No model turn has started. Do not report this as an agent timeout
                # or create a replacement while the previous actor may still exist.
                raise AgentRunError(
                    f"Timed out deleting previous AX task {task_name} after {exc.timeout:g}s; "
                    "session restart blocked before worker startup. "
                    "Check AX task status and ax-controller logs for cleanup errors."
                ) from None
        chat = AxChat(self, task_name,
                      (role_config or {}).get("timeout_seconds",
                                              RUNTIME["worker"]["default_timeout_seconds"]))
        try:
            self._run("apply", "-f", "-", input=json.dumps(self.workspace_manifest()),
                      timeout=remaining(AX_CONFIG["ax"]["cli_timeout_seconds"]))
            self._run("apply", "-f", "-", input=json.dumps(self.task_manifest(chat_id, role_config)),
                      timeout=remaining(AX_CONFIG["ax"]["cli_timeout_seconds"]))
            return self._wait_for_chat(chat, remaining, deadline)
        except Exception:
            chat.close()
            raise

    def _wait_for_chat(self, chat: AxChat, remaining, outer_deadline: float | None) -> AxChat:
        deadline = time.monotonic() + AX_CONFIG["ax"]["startup_timeout_seconds"]
        if outer_deadline is not None:
            deadline = min(deadline, outer_deadline)
        task_name = chat.container_name
        last_phase = None
        last_reason = None
        last_report = None
        while True:
            if chat.is_alive(timeout_seconds=remaining(15)):
                return chat
            try:
                phase, reason = self.task_status(task_name, timeout=remaining(15))
            except (AgentRunError, subprocess.TimeoutExpired, OSError):
                phase, reason = None, None
            report = (phase, reason, chat.probe_issue)
            if phase and report != last_report:
                print(f"AX task {task_name}: phase={phase} reason={reason or 'none'} "
                      f"probe={chat.probe_issue or 'waiting'}",
                      file=sys.stderr, flush=True)
                last_report = report
            if phase:
                last_phase, last_reason = phase, reason
            if phase in {"Failed", "Completed", "Terminating"}:
                chat.close()
                detail = f"AX task entered {phase} before chat worker became ready"
                if reason:
                    detail += f" (reason: {reason})"
                raise AgentRunError(detail)
            if time.monotonic() >= deadline:
                break
            time.sleep(remaining(AX_CONFIG["ax"]["startup_probe_interval_seconds"]))
        chat.close()
        detail = "AX chat worker did not become ready"
        if last_phase:
            detail += f" (last phase: {last_phase}"
            if last_reason:
                detail += f", reason: {last_reason}"
            if chat.probe_issue:
                detail += f", probe: {chat.probe_issue}"
            detail += ")"
        elif chat.probe_issue:
            detail += f" (probe: {chat.probe_issue})"
        raise AgentRunError(detail)

    def close_session(self, session_id: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{16}", session_id):
            raise ValueError("invalid AX session ID")
        AxChat(self, f"popot-chat-{session_id}", 1).close()


@dataclass
class AxChat:
    runner: AxAgentRunner
    container_name: str
    timeout_seconds: int
    workspace_path: None = None
    closed: bool = False
    probe_issue: str | None = None
    _close_lock: object = field(default_factory=threading.Lock, repr=False)

    def is_alive(self, timeout_seconds: float = 15) -> bool:
        if self.closed:
            return False
        try:
            ready = self.runner.remote(self.container_name, {"action": "ping"},
                                       timeout=timeout_seconds).get("status") == "ok"
            self.probe_issue = None if ready else "worker ping did not succeed"
            return ready
        except AgentRunError as exc:
            detail = str(exc).lower()
            if "connection refused" in detail:
                self.probe_issue = "AX ssh connection failed"
            elif "guest services" in detail:
                self.probe_issue = "AX guest services unavailable"
            elif "no response" in detail:
                self.probe_issue = "AX remote client returned no response"
            elif "phase" in detail:
                self.probe_issue = "AX task is not running"
            else:
                self.probe_issue = "AX ssh failed"
            return False
        except subprocess.TimeoutExpired:
            self.probe_issue = "AX ssh timed out"
            return False
        except OSError:
            self.probe_issue = "AX ssh OS error"
            return False

    def restore(self, messages: list[dict[str, str]]) -> None:
        result = self.runner.remote(self.container_name,
                                    {"action": "restore", "messages": messages},
                                    timeout=30)
        if result.get("status") != "ok":
            raise AgentRunError(result.get("error", "AX chat worker could not restore history"))

    def send(self, message: str, *, timeout_seconds: float | None = None) -> dict[str, str]:
        timeout = self.timeout_seconds if timeout_seconds is None else min(
            self.timeout_seconds, timeout_seconds)
        service = self.runner.delegation
        with (service.turn(self.container_name.removeprefix("popot-chat-"), timeout)
              if service else nullcontext()):
            result = self.runner.remote(self.container_name,
                                        {"action": "message", "message": message},
                                        timeout=timeout + 10)
        if isinstance(result.get("error"), str):
            raise AgentRunError(result["error"])
        if not isinstance(result.get("answer"), str):
            raise AgentRunError("AX chat worker returned no answer")
        return {"answer": result["answer"]}

    def close(self) -> None:
        with self._close_lock:
            if self.closed:
                return
            self.closed = True
            if self.runner.delegation:
                self.runner.delegation.revoke(self.container_name.removeprefix("popot-chat-"))
            try:
                result = self.runner._run("delete", "task", self.container_name,
                                          timeout=5, allow_failure=True)
                if result.returncode:
                    self._report_cleanup_failure(f"CLI exit {result.returncode}")
            except (AgentRunError, AgentConfigurationError, subprocess.TimeoutExpired, OSError) as exc:
                self._report_cleanup_failure(type(exc).__name__)

    def _report_cleanup_failure(self, reason: str) -> None:
        # CLI output and exception strings can include environment or command data.
        print(f"AX task cleanup not confirmed: task={self.container_name} reason={reason}; "
              "check AX task status and ax-controller logs",
              file=sys.stderr, flush=True)

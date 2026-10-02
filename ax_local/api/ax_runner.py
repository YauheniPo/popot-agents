"""AX Task adapter for the existing HTTP chat API."""

from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass

from popot_agents.orchestrator.main import AgentConfigurationError, AgentRunError
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
        }
        if self.proxy_token:
            env["HARNESS_API_KEY_ENV"] = "AX_PROXY_TOKEN"
            env["AX_PROXY_TOKEN"] = self.proxy_token
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

    def task_status(self, task_name: str) -> tuple[str | None, str | None]:
        """Read only AX status fields; never expose Task environment values."""
        result = self._run("describe", "task", task_name, timeout=15, allow_failure=True)
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

    def start_chat(self, chat_id: str, role_config: dict | None = None) -> AxChat:
        task_name = f"popot-chat-{chat_id}"
        existing = self._run("get", "task", task_name, allow_failure=True)
        if existing.returncode and "NotFound" not in existing.stderr:
            raise AgentRunError("AX task lookup failed")
        if not existing.returncode:
            # A previous API process may have left a task with an expired proxy token.
            self._run("delete", "task", task_name, timeout=60)
        self._run("apply", "-f", "-",
                  input=json.dumps(self.workspace_manifest()))
        self._run("apply", "-f", "-",
                  input=json.dumps(self.task_manifest(chat_id, role_config)))
        chat = AxChat(self, task_name,
                      (role_config or {}).get("timeout_seconds",
                                              RUNTIME["worker"]["default_timeout_seconds"]))
        deadline = time.monotonic() + AX_CONFIG["ax"]["startup_timeout_seconds"]
        last_phase = None
        last_reason = None
        last_report = None
        while True:
            if chat.is_alive():
                return chat
            try:
                phase, reason = self.task_status(task_name)
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
            time.sleep(AX_CONFIG["ax"]["startup_probe_interval_seconds"])
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


@dataclass
class AxChat:
    runner: AxAgentRunner
    container_name: str
    timeout_seconds: int
    workspace_path: None = None
    closed: bool = False
    probe_issue: str | None = None

    def is_alive(self) -> bool:
        if self.closed:
            return False
        try:
            ready = self.runner.remote(self.container_name, {"action": "ping"},
                                       timeout=15).get("status") == "ok"
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

    def send(self, message: str) -> dict[str, str]:
        result = self.runner.remote(self.container_name,
                                    {"action": "message", "message": message},
                                    timeout=self.timeout_seconds)
        if isinstance(result.get("error"), str):
            raise AgentRunError(result["error"])
        if not isinstance(result.get("answer"), str):
            raise AgentRunError("AX chat worker returned no answer")
        return {"answer": result["answer"]}

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self.runner._run("delete", "task", self.container_name,
                                 timeout=30,
                                 allow_failure=True)
            except (AgentRunError, AgentConfigurationError, subprocess.TimeoutExpired, OSError):
                pass

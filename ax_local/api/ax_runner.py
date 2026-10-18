"""AX Task adapter for the existing HTTP chat API."""

from __future__ import annotations

import base64
from contextlib import nullcontext
import json
import os
import re
import selectors
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from popot_agents.orchestrator.main import AgentConfigurationError, AgentRunError, AgentResumeError
from popot_agents.role_env import selected_role_env
from ax_local.config import AX_CONFIG
from popot_agents.runtime_config import RUNTIME, validate_model_parameters
from popot_agents.progress import is_progress_message, emit_progress, progress_observer
from ..worker.remote_client import RESPONSE_PREFIX, PROGRESS_PREFIX
from .worker_queue import WorkerQueue


class AxAgentRunner:
    session_mode = "http"
    retain_session_history = True
    queues_worker_start = True

    def __init__(self, image: str, model: str, base_url: str,
                 context: str = AX_CONFIG["context"],
                 proxy_token: str | None = None, model_parameters: dict | None = None,
                 worker_queue: WorkerQueue | None = None) -> None:
        if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image):
            raise ValueError("AX worker image must be pinned by sha256 digest")
        if not context.startswith("kind-") or not re.fullmatch(r"[a-z0-9-]+", context):
            raise ValueError("AX requires a local kind context")
        if not model or not base_url.startswith(("http://", "https://")):
            raise ValueError("a model and HTTP base URL are required")
        self.image = image
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.context = context
        self.snapshot_mode = AX_CONFIG['snapshot_mode']
        self.proxy_token = proxy_token
        self.model_parameters = validate_model_parameters(
            {} if model_parameters is None else model_parameters, 'model_parameters')
        self.delegation = None
        self.worker_queue = worker_queue if worker_queue is not None else WorkerQueue(
            AX_CONFIG['max_workers'], AX_CONFIG['worker_queue_timeout_seconds'])
        self._cleanup_pending = {}
        self._cleanup_lock = threading.RLock()

    def _run(self, *args: str, input: str | None = None,
             timeout: int = AX_CONFIG["cli_timeout_seconds"],
             allow_failure: bool = False) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                ["ax", f"--context={self.context}",
                 f"--atespace={AX_CONFIG['atespace']}", *args], input=input,
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
            "metadata": {"name": AX_CONFIG["workspace_name"],
                         "atespace": AX_CONFIG["atespace"]},
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
            "HARNESS_CHAT_ID": chat_id,
            "HARNESS_SOCKET_PATH": "/workspace/chat.sock",
            "POPOT_SESSION_SNAPSHOT": self.snapshot_mode,
            "POPOT_RESOURCE_METRICS_URL": "http://opentelemetry-collector.otel-system.svc:4318/v1/metrics",
            "POPOT_RESOURCE_METRICS_TASK": f"popot-chat-{chat_id}",
            "POPOT_RESOURCE_METRICS_ROLE": role_config.get("_ax_role") or "unassigned",
            "PYTHONPATH": "/app",
            "HOME": "/workspace",
            "XDG_CACHE_HOME": "/workspace/.cache",
            "XDG_CONFIG_HOME": "/workspace/.config",
            "HARNESS_TURN_TIMEOUT_SECONDS": str(max(RUNTIME["timeouts"]["min_turn_seconds"], role_config.get(
                "timeout_seconds", RUNTIME["timeouts"]["turn_seconds"])
                - RUNTIME["timeouts"]["turn_response_margin_seconds"])),
        }
        if self.model_parameters:
            env['HARNESS_MODEL_PARAMETERS_JSON'] = json.dumps(self.model_parameters)
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
            # Docker DNS names are not available inside AX's Substrate sandbox.
            # Cognee also joins kind; resolve its kind-network address in the API.
            if 'COGNEE_API_URL' in env:
                memory_url = urlsplit(env['COGNEE_API_URL'])
                if memory_url.hostname == 'cognee':
                    try:
                        memory_host = socket.gethostbyname('cognee')
                    except socket.gaierror:
                        # Memory is optional. Its tools will report the outage;
                        # a stopped memory service must not block the main task.
                        pass
                    else:
                        env['COGNEE_API_URL'] = urlunsplit(memory_url._replace(
                            netloc=f"{memory_host}:{memory_url.port or 8000}"))
            env["HARNESS_TOOL_ENV_NAMES_JSON"] = json.dumps(names)
        except ValueError as exc:
            raise AgentConfigurationError(str(exc)) from exc
        return {
            "apiVersion": "ax.io/v1alpha1", "kind": "Task",
            "metadata": {"name": f"popot-chat-{chat_id}",
                         "atespace": AX_CONFIG["atespace"]},
            "spec": {
                "image": self.image,
                "command": ["python", "-u", "-m", "ax_local.worker.entrypoint"],
                "env": [{"name": key, "value": value} for key, value in env.items()],
                "resources": AX_CONFIG["task_resources"],
                "workspaces": [{"name": AX_CONFIG["workspace_name"],
                                "path": "/workspace"}],
                "debug": AX_CONFIG["task_debug"],
            },
        }

    def remote(self, task_name: str, payload: dict, timeout: int = 60, *, on_progress=None) -> dict:
        raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        if len(raw) + 1 > RUNTIME['limits']['socket_message_bytes']:
            raise AgentRunError('AX request is too large')
        encoded = base64.urlsafe_b64encode(raw).decode('ascii')
        if len(encoded) <= 48000:
            return self._remote_command(task_name, encoded, timeout=timeout, on_progress=on_progress)
        deadline = time.monotonic() + timeout

        def remaining():
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise subprocess.TimeoutExpired(['ax', 'ssh'], timeout)
            return budget

        request_id = uuid.uuid4().hex
        for offset in range(0, len(encoded), 48000):
            self._remote_command(task_name, "--chunk", request_id,
                                 encoded[offset:offset + 48000], timeout=remaining())
        return self._remote_command(task_name, "--consume", request_id, timeout=remaining(), on_progress=on_progress)

    def _remote_command(self, task_name: str, *arguments: str, timeout: int, on_progress=None) -> dict:
        args = ("ssh", task_name, "--", "python", "/app/ax_local/worker/remote_client.py", *arguments)
        result = (self._stream_command(args, timeout, on_progress) if on_progress
                  else self._run(*args, timeout=timeout))
        for line in reversed(result.stdout.splitlines()):
            if line.startswith(RESPONSE_PREFIX):
                try:
                    answer = json.loads(line[len(RESPONSE_PREFIX):])
                except json.JSONDecodeError as exc:
                    raise AgentRunError("AX worker returned invalid JSON") from exc
                if isinstance(answer, dict):
                    return answer
        raise AgentRunError("AX worker returned no response")

    def _stream_command(self, args, timeout, on_progress):
        """Drain both CLI pipes with a wall-clock deadline and bounded buffers."""
        command = ['ax', f'--context={self.context}', f'--atespace={AX_CONFIG["atespace"]}', *args]
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, start_new_session=True)
        except FileNotFoundError as exc:
            raise AgentConfigurationError('AX CLI is not installed') from exc
        deadline = time.monotonic() + timeout
        pending, answer = b'', None
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, 'out')
                selector.register(process.stderr, selectors.EVENT_READ, 'err')
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(['ax', 'ssh'], timeout)
                    for key, _ in selector.select(min(remaining, .2)):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        if key.data == 'err':
                            continue  # Drain without retaining CLI diagnostics/credentials.
                        pending += chunk
                        if len(pending) > 8 * 1024 * 1024:
                            raise AgentRunError('AX worker response is too large')
                        while b'\n' in pending:
                            line, pending = pending.split(b'\n', 1)
                            if line.startswith(PROGRESS_PREFIX.encode()):
                                try:
                                    message = json.loads(line[len(PROGRESS_PREFIX):])
                                except (ValueError, UnicodeDecodeError):
                                    continue
                                if is_progress_message(message):
                                    on_progress(message)
                            elif line.startswith(RESPONSE_PREFIX.encode()):
                                answer = line.decode('utf-8')
                code = process.wait(timeout=max(.001, deadline - time.monotonic()))
                if code:
                    raise AgentRunError('AX command failed while receiving agent progress')
                return subprocess.CompletedProcess(command, code, answer or '', '')
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=3)
            process.stdout.close()
            process.stderr.close()

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
        if not re.fullmatch(r"[0-9a-f]{16}", chat_id):
            raise ValueError("invalid session ID")
        task_name = f"popot-chat-{chat_id}"
        emit_progress('waiting_for_worker')
        lease = self.worker_queue.acquire(task_name, deadline=deadline, cancel_event=cancel_event)
        try:
            emit_progress('preparing_workspace')
            return self._start_reserved_chat(chat_id, role_config, lease,
                                             deadline=deadline, cancel_event=cancel_event)
        except Exception:
            # If cleanup was not confirmed, maintenance still owns this lease.
            with self._cleanup_lock:
                pending = task_name in self._cleanup_pending
            if not pending:
                self.worker_queue.release(task_name, lease)
            raise

    def _start_reserved_chat(self, chat_id, role_config, lease,
                             *, deadline=None, cancel_event=None) -> AxChat:
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
                             timeout=remaining(AX_CONFIG["cli_timeout_seconds"]))
        if existing.returncode and "NotFound" not in existing.stderr:
            raise AgentRunError("AX task lookup failed")
        if not existing.returncode:
            # A previous API process may have left a task with an expired proxy token.
            try:
                self._run("delete", "task", task_name,
                          timeout=remaining(AX_CONFIG["cli_timeout_seconds"]))
            except subprocess.TimeoutExpired as exc:
                self._defer_cleanup(task_name, lease)
                # No model turn has started. Do not report this as an agent timeout
                # or create a replacement while the previous actor may still exist.
                raise AgentRunError(
                    f"Timed out deleting previous AX task {task_name} after {exc.timeout:g}s; "
                    "session restart blocked before worker startup. "
                    "Check AX task status and ax-controller logs for cleanup errors."
                ) from None
            except (AgentRunError, AgentConfigurationError, OSError):
                self._defer_cleanup(task_name, lease)
                raise
        chat = AxChat(self, task_name,
                      (role_config or {}).get("timeout_seconds",
                                              RUNTIME["timeouts"]["turn_seconds"]),
                      slot_token=lease)
        try:
            self._run("apply", "-f", "-", input=json.dumps(self.workspace_manifest()),
                      timeout=remaining(AX_CONFIG["cli_timeout_seconds"]))
            self._run("apply", "-f", "-", input=json.dumps(self.task_manifest(chat_id, role_config)),
                      timeout=remaining(AX_CONFIG["cli_timeout_seconds"]))
            return self._wait_for_chat(chat, remaining, deadline)
        except Exception:
            chat.close()
            raise

    def _wait_for_chat(self, chat: AxChat, remaining, outer_deadline: float | None,
                       *, resuming: bool = False) -> AxChat:
        deadline = time.monotonic() + AX_CONFIG["startup_timeout_seconds"]
        if outer_deadline is not None:
            deadline = min(deadline, outer_deadline)
        task_name = chat.container_name
        last_phase = None
        last_reason = None
        last_report = None
        while True:
            if chat.is_alive(timeout_seconds=remaining(AX_CONFIG["probe_timeout_seconds"])):
                return chat
            try:
                phase, reason = self.task_status(task_name, timeout=remaining(AX_CONFIG["probe_timeout_seconds"]))
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
                if not resuming:
                    chat.close()
                detail = f"AX task entered {phase} before chat worker became ready"
                if reason:
                    detail += f" (reason: {reason})"
                raise AgentRunError(detail)
            if resuming and self.snapshot_mode == 'full' and phase == "Running" \
                    and chat.probe_issue == "AX chat socket unavailable":
                # A full checkpoint already contains the listening socket. If
                # SSH works but that socket is gone, the restored child exited.
                raise AgentRunError("AX resumed task lost its chat process")
            if time.monotonic() >= deadline:
                break
            time.sleep(remaining(AX_CONFIG["startup_probe_interval_seconds"]))
        if not resuming:
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

    def _defer_cleanup(self, task_name, lease) -> None:
        with self._cleanup_lock:
            self._cleanup_pending[task_name] = lease

    def _release_worker(self, task_name, lease) -> None:
        with self._cleanup_lock:
            if lease is None or self._cleanup_pending.get(task_name) is lease:
                self._cleanup_pending.pop(task_name, None)
        self.worker_queue.release(task_name, lease)

    def _delete_worker(self, task_name, lease):
        # Serialize retries with explicit close operations. A copied cleanup
        # entry must never issue a late delete against a replacement worker.
        with self._cleanup_lock:
            try:
                result = self._run('delete', 'task', task_name,
                                   timeout=AX_CONFIG['delete_timeout_seconds'], allow_failure=True)
            except (AgentRunError, AgentConfigurationError, subprocess.TimeoutExpired, OSError):
                self._defer_cleanup(task_name, lease)
                raise
            if result.returncode and 'NotFound' not in result.stderr:
                self._defer_cleanup(task_name, lease)
            else:
                self._release_worker(task_name, lease)
            return result

    def maintenance(self) -> None:
        with self._cleanup_lock:
            pending = tuple(self._cleanup_pending.items())
        for task_name, lease in pending:
            with self._cleanup_lock:
                if task_name not in self._cleanup_pending or self._cleanup_pending[task_name] is not lease:
                    continue
                try:
                    self._delete_worker(task_name, lease)
                except (AgentRunError, AgentConfigurationError, subprocess.TimeoutExpired, OSError):
                    pass  # Keep capacity reserved until AX confirms deletion.

    def shutdown_worker_queue(self) -> None:
        self.worker_queue.close()

    def recover_workers(self) -> None:
        """Remove previous-process Tasks before accepting requests with a new proxy token."""
        result = self._run('get', 'tasks')
        names = [line.split()[0] for line in result.stdout.splitlines()
                 if line.split() and re.fullmatch(r'popot-chat-[0-9a-f]{16}', line.split()[0])]
        for name in names:
            # Failure blocks startup, so an orphan cannot bypass the new limit.
            self._run('delete', 'task', name)


@dataclass
class AxChat:
    runner: AxAgentRunner
    container_name: str
    timeout_seconds: int
    workspace_path: None = None
    closed: bool = False
    suspended: bool = False
    probe_issue: str | None = None
    slot_token: object = field(default=None, repr=False)
    _close_lock: object = field(default_factory=threading.RLock, repr=False)
    _messages: list[dict[str, str]] = field(default_factory=list, repr=False)
    _snapshot_issue: str | None = field(default=None, repr=False)

    @property
    def idle_timeout_seconds(self) -> int:
        return AX_CONFIG["suspend_retention_seconds"]

    @property
    def status(self) -> str:
        if self.closed:
            return 'stopped'
        return self._snapshot_issue or ('suspended' if self.suspended else 'running')

    def after_turn(self) -> None:
        """Checkpoint only after the orchestrator has committed the conversation."""
        with self._close_lock:
            if self.closed or self.suspended:
                return
            started = time.monotonic()
            deadline = started + AX_CONFIG["cli_timeout_seconds"]
            emit_progress('suspending')
            try:
                self.runner._run("suspend", "task", self.container_name,
                                 timeout=AX_CONFIG["cli_timeout_seconds"])
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("AX suspension deadline exceeded")
                    phase, _ = self.runner.task_status(
                        self.container_name,
                        timeout=min(remaining, AX_CONFIG["probe_timeout_seconds"]))
                    if phase == "Suspended":
                        self.suspended = True
                        self._snapshot_issue = None
                        elapsed = round(time.monotonic() - started, 3)
                        emit_progress('snapshot_saved', mode=self.runner.snapshot_mode,
                                      elapsed_seconds=elapsed)
                        print(f"AX task suspended: task={self.container_name} "
                              f"snapshot={self.runner.snapshot_mode} elapsed_seconds={elapsed}",
                              file=sys.stderr, flush=True)
                        return
                    if phase in {"Failed", "Completed", "Terminating"}:
                        raise AgentRunError("AX task could not suspend")
                    time.sleep(min(AX_CONFIG["startup_probe_interval_seconds"],
                                   max(0, deadline - time.monotonic())))
            except (AgentRunError, AgentConfigurationError, subprocess.TimeoutExpired, OSError) as exc:
                # A timeout leaves the phase uncertain. Require an explicit
                # resume/check before the next turn, retaining files and lease.
                self.suspended = True
                self._snapshot_issue = 'snapshot_unconfirmed'
                emit_progress('snapshot_unconfirmed', mode=self.runner.snapshot_mode)
                print(f"AX suspension not confirmed: task={self.container_name} "
                      f"reason={type(exc).__name__}; task retained for retry",
                      file=sys.stderr, flush=True)

    def resume(self) -> None:
        with self._close_lock:
            if self.closed:
                raise AgentRunError("AX chat worker is closed")
            if not self.suspended:
                return
            emit_progress('resuming')
            started = time.monotonic()
            deadline = started + AX_CONFIG["startup_timeout_seconds"]

            def remaining(limit):
                budget = deadline - time.monotonic()
                if budget <= 0:
                    raise TimeoutError("AX resume deadline exceeded")
                return min(limit, budget)

            try:
                self.runner._run("resume", "task", self.container_name,
                                 timeout=remaining(AX_CONFIG["cli_timeout_seconds"]))
                self.suspended = False
                self.runner._wait_for_chat(self, remaining, deadline, resuming=True)
                if self.runner.snapshot_mode == 'data':
                    # DATA restores the actor's workspace and boots a new chat
                    # process. Replay only the committed dialogue, never a turn.
                    self.restore(self._messages, timeout_seconds=remaining(
                        AX_CONFIG['restore_timeout_seconds']))
                self._snapshot_issue = None
                elapsed = round(time.monotonic() - started, 3)
                emit_progress('snapshot_restored', mode=self.runner.snapshot_mode,
                              elapsed_seconds=elapsed)
                print(f"AX task resumed: task={self.container_name} "
                      f"snapshot={self.runner.snapshot_mode} elapsed_seconds={elapsed}",
                      file=sys.stderr, flush=True)
            except (AgentRunError, AgentConfigurationError, subprocess.TimeoutExpired, OSError):
                self.suspended = True
                self._snapshot_issue = 'resume_failed'
                emit_progress('snapshot_restore_failed', mode=self.runner.snapshot_mode)
                raise AgentResumeError(
                    "AX chat worker could not resume; task and checkpoint retained; "
                    "retry with the same sessionId"
                ) from None

    def is_alive(self, timeout_seconds: float = AX_CONFIG["probe_timeout_seconds"]) -> bool:
        if self.closed:
            return False
        if self.suspended:
            # Inspecting/listing a chat must never wake its sandbox with SSH.
            return True
        try:
            ready = self.runner.remote(self.container_name, {"action": "ping"},
                                       timeout=timeout_seconds).get("status") == "ok"
            self.probe_issue = None if ready else "worker ping did not succeed"
            return ready
        except AgentRunError as exc:
            detail = str(exc).lower()
            if "filenotfounderror" in detail or "connectionrefusederror" in detail:
                self.probe_issue = "AX chat socket unavailable"
            elif "connection refused" in detail:
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

    def restore(self, messages: list[dict[str, str]], *, timeout_seconds: float | None = None) -> None:
        messages = [dict(item) for item in messages]
        result = self.runner.remote(self.container_name,
                                    {"action": "restore", "messages": messages},
                                    timeout=(AX_CONFIG["restore_timeout_seconds"]
                                             if timeout_seconds is None else timeout_seconds))
        if result.get("status") != "ok":
            raise AgentRunError(result.get("error", "AX chat worker could not restore history"))
        self._messages = messages

    def send(self, message: str, *, timeout_seconds: float | None = None) -> dict[str, str]:
        self.resume()
        timeout = self.timeout_seconds if timeout_seconds is None else min(
            self.timeout_seconds, timeout_seconds)
        service = self.runner.delegation
        with (service.turn(self.container_name.removeprefix("popot-chat-"), timeout)
              if service else nullcontext()):
            callback = progress_observer()
            payload = {"action": "message", "message": message}
            if callback:
                payload['progress'] = True
            result = self.runner.remote(self.container_name,
                                        payload,
                                        timeout=timeout + AX_CONFIG["response_margin_seconds"],
                                        **({'on_progress': callback} if callback else {}))
        if isinstance(result.get("error"), str):
            raise AgentRunError(result["error"])
        if not isinstance(result.get("answer"), str):
            raise AgentRunError("AX chat worker returned no answer")
        if self.runner.snapshot_mode == 'data':
            self._messages = self._messages + [
                {'role': 'user', 'content': message.strip()},
                {'role': 'assistant', 'content': result['answer']},
            ]
        return {"answer": result["answer"]}

    def close(self) -> None:
        with self._close_lock:
            if self.closed:
                return
            self.closed = True
            if self.runner.delegation:
                self.runner.delegation.revoke(self.container_name.removeprefix("popot-chat-"))
            try:
                result = self.runner._delete_worker(self.container_name, self.slot_token)
                if result.returncode and 'NotFound' not in result.stderr:
                    self._report_cleanup_failure(f"CLI exit {result.returncode}")
            except (AgentRunError, AgentConfigurationError, subprocess.TimeoutExpired, OSError) as exc:
                self._report_cleanup_failure(type(exc).__name__)

    def _report_cleanup_failure(self, reason: str) -> None:
        # CLI output and exception strings can include environment or command data.
        print(f"AX task cleanup not confirmed: task={self.container_name} reason={reason}; "
              "check AX task status and ax-controller logs",
              file=sys.stderr, flush=True)

"""Local A2A 1.0 JSON-RPC task adapter with per-actor delegation authority.

Child sessions use the existing session store and AX runner. No provider keys
or delegation capabilities are persisted in transcripts or role configuration.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from popot_agents.runtime_config import RUNTIME
from popot_agents.orchestrator.main import AgentConfigurationError, AgentRunError

ACTIVE = {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}


def _worker_failure(exc: Exception, stage: str) -> tuple[str, dict]:
    """Expose known diagnostics without forwarding arbitrary exception text."""
    code, reason = "worker_failed", "delegated AX worker failed; inspect local worker logs"
    if isinstance(exc, subprocess.TimeoutExpired):
        code, reason = "ax_command_timeout", "AX command timed out"
    elif isinstance(exc, AgentConfigurationError):
        code, reason = "worker_configuration_failed", "AX worker configuration failed"
    elif isinstance(exc, AgentRunError):
        detail = str(exc)
        model_http = re.fullmatch(r"model endpoint returned HTTP ([0-9]{3})", detail)
        mcp = re.match(r"MCP server ([a-zA-Z0-9_-]{1,64}) "
                       r"(failed|timed out) during discovery(?:$|:)", detail)
        if model_http:
            code, reason = "model_http_error", model_http[0]
        elif mcp:
            code = "mcp_discovery_failed"
            reason = f"MCP server {mcp[1]} {mcp[2]} during discovery"
        elif detail in {"model endpoint is unreachable", "model endpoint returned no answer",
                        "model endpoint returned an empty answer"}:
            code, reason = "model_response_failed", detail
    return reason, {"stage": stage, "code": code, "error_type": type(exc).__name__}


class A2AError(ValueError):
    def __init__(self, message: str, code: int = -32602):
        super().__init__(message)
        self.code = code


@dataclass
class Run:
    deadline: float
    canceled: threading.Event = field(default_factory=threading.Event)
    calls: int = 0


@dataclass
class Grant:
    session_id: str
    role: str
    root_id: str
    lineage: tuple[str, ...]
    token: str = field(repr=False)
    run: Run | None = None
    active: bool = False
    deadline: float = 0


@dataclass
class Job:
    session_id: str
    config: dict
    task: str
    run: Run
    deadline: float
    canceled: threading.Event = field(default_factory=threading.Event)
    chat: object = None
    thread: threading.Thread | None = None
    lock: object = field(default_factory=threading.RLock)


class DelegationService:
    def __init__(self, roles: dict, policy: dict, runner, store, base_url: str):
        self.roles, self.policy = roles, policy
        self.runner, self.store = runner, store
        self.base_url = base_url.rstrip("/")
        self.lock = threading.RLock()
        self.grants: dict[str, Grant] = {}
        self.tokens: dict[str, Grant] = {}
        self.jobs: dict[str, Job] = {}
        self.slots = threading.BoundedSemaphore(policy["max_concurrent_tasks"])
        self.closed = False

    def is_active(self, session_id: str) -> bool:
        with self.lock:
            return session_id in self.jobs

    def recover_interrupted(self) -> None:
        """An API restart cannot resume a model's pending tool call safely."""
        for saved in self.store.list_chats():
            config = saved.get("roleConfig") or {}
            meta = config.get("_ax_delegation")
            if meta and meta.get("state") in ACTIVE:
                self.runner.close_session(saved["sessionId"])
                meta.update(state="TASK_STATE_FAILED", error="AX API restarted during delegation")
                self.store.save(saved["sessionId"], saved["agent"], saved["messages"],
                                saved["role"], config)

    def prepare_config(self, session_id: str, config: dict | None) -> dict:
        config = dict(config or {})
        role = config.get("_ax_role")
        if role is None:
            saved = self.store.get(session_id)
            role = saved.get("role") if saved else None
        if role in self.roles:
            config["_ax_role"] = role
            if self.roles[role].get("allowed_roles"):
                config["timeout_seconds"] = max(config.get("timeout_seconds", 60),
                                                self.policy["turn_timeout_seconds"])
        return config

    def worker_environment(self, session_id: str, config: dict) -> dict[str, str]:
        role = config.get("_ax_role")
        if role not in self.roles:
            return {}
        with self.lock:
            grant = self.grants.get(session_id)
            if grant is None:
                meta = config.get("_ax_delegation", {})
                job = self.jobs.get(session_id)
                grant = Grant(session_id, role, meta.get("root_session_id", session_id),
                              tuple(meta.get("lineage", [role])), secrets.token_urlsafe(32),
                              run=job.run if job else None)
                self.grants[session_id] = grant
                self.tokens[hashlib.sha256(grant.token.encode()).hexdigest()] = grant
            allowed = self.roles[role].get("allowed_roles", [])
            if not allowed:
                return {}
            return {"AX_DELEGATION_URL": self.base_url, "AX_DELEGATION_TOKEN": grant.token,
                    "AX_DELEGATION_ROLES": json.dumps(allowed)}

    def revoke(self, session_id: str) -> None:
        with self.lock:
            grant = self.grants.pop(session_id, None)
            if grant:
                self.tokens.pop(hashlib.sha256(grant.token.encode()).hexdigest(), None)

    @contextmanager
    def turn(self, session_id: str, timeout_seconds: float):
        with self.lock:
            grant = self.grants.get(session_id)
            if grant is None:
                yield_without_grant = True
                owner = False
            else:
                yield_without_grant = False
                if grant.active:
                    raise RuntimeError("AX session already has an active turn")
                job = self.jobs.get(session_id)
                if job and (job.canceled.is_set() or job.run.canceled.is_set()):
                    raise TimeoutError("AX parent turn was canceled")
                owner = grant.run is None
                if owner:
                    grant.root_id, grant.lineage = session_id, (grant.role,)
                    grant.run = Run(time.monotonic() + timeout_seconds)
                grant.active = True
                grant.deadline = min(grant.run.deadline, time.monotonic() + timeout_seconds)
                run = grant.run
        try:
            yield
        finally:
            if not yield_without_grant:
                with self.lock:
                    grant.active = False
                    if owner:
                        grant.run = None
                if owner:
                    self._cancel_run(run)
                else:
                    self._cancel_children(session_id)

    def _cancel_children(self, parent_id: str) -> None:
        with self.lock:
            descendants = {parent_id}
            selected = []
            for _ in range(self.policy["max_depth"] + 1):
                for job in self.jobs.values():
                    if job.session_id not in descendants and \
                            job.config["_ax_delegation"]["parent_session_id"] in descendants:
                        descendants.add(job.session_id)
                        selected.append(job)
        for job in selected:
            job.canceled.set()
            if job.chat:
                job.chat.close()

    def _cancel_run(self, run: Run) -> None:
        run.canceled.set()
        with self.lock:
            jobs = [job for job in self.jobs.values() if job.run is run]
        for job in jobs:
            job.canceled.set()
            if job.chat:
                job.chat.close()

    def _authorized(self, token: str, role: str) -> Grant:
        with self.lock:
            grant = self.tokens.get(hashlib.sha256(token.encode()).hexdigest())
            if self.closed or not grant or not grant.active or not grant.run \
                    or grant.run.canceled.is_set() or time.monotonic() >= grant.deadline:
                raise A2AError("delegation capability is expired or invalid")
            if role not in self.roles[grant.role].get("allowed_roles", []):
                raise A2AError("target role is not allowed")
            return grant

    def card(self, role: str) -> dict:
        if role not in self.roles:
            raise A2AError("unknown role")
        return {"name": f"popot AX {role}", "description": self.roles[role]["instructions"],
                "version": "0.1.0", "supportedInterfaces": [{
                    "url": f"{self.base_url}/a2a/{role}", "protocolBinding": "JSONRPC",
                    "protocolVersion": "1.0"}],
                "capabilities": {"streaming": False, "pushNotifications": False},
                "securitySchemes": {"actor": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}},
                "securityRequirements": [{"schemes": {"actor": {"list": []}}}],
                "defaultInputModes": ["text/plain"], "defaultOutputModes": ["text/plain"],
                "skills": [{"id": role, "name": role, "description": self.roles[role]["instructions"],
                            "tags": [role]}]}

    def dispatch(self, token: str, role: str, method: str, params: dict) -> dict:
        grant = self._authorized(token, role)
        if not isinstance(params, dict):
            raise A2AError("params must be an object")
        if method == "SendMessage":
            return {"task": self._submit(grant, role, params)}
        if method in {"GetTask", "CancelTask"}:
            if not isinstance(params.get("id"), str):
                raise A2AError("task id must be a string")
            saved = self.store.get(params["id"])
            meta = (saved.get("roleConfig") or {}).get("_ax_delegation", {}) if saved else {}
            if not saved or saved["role"] != role or meta.get("parent_session_id") != grant.session_id:
                raise A2AError("task not found", -32001)
            if method == "CancelTask":
                with self.lock:
                    job = self.jobs.get(saved["sessionId"])
                if not job:
                    raise A2AError("task is no longer cancelable", -32002)
                with job.lock:
                    if job.config["_ax_delegation"]["state"] not in ACTIVE:
                        raise A2AError("task is no longer cancelable", -32002)
                    job.canceled.set()
                    self._save(job, "TASK_STATE_CANCELED", error="delegation canceled")
                if job.chat:
                    job.chat.close()
                self._cancel_children(job.session_id)
                saved = self.store.get(params["id"])
            return self._task(saved)
        raise A2AError("operation is not supported by the local AX adapter", -32601)

    def _submit(self, grant: Grant, role: str, params: dict) -> dict:
        message = params.get("message")
        if not isinstance(message, dict) or message.get("role") != "ROLE_USER":
            raise A2AError("message with ROLE_USER is required")
        message_id = message.get("messageId")
        if not isinstance(message_id, str) or not 1 <= len(message_id) <= 100:
            raise A2AError("messageId must be a nonempty string up to 100 characters")
        if "taskId" in message or message.get("contextId", grant.root_id) != grant.root_id:
            raise A2AError("only new tasks in the caller's context are supported")
        parts = message.get("parts")
        if not isinstance(parts, list) or not parts or any(
                not isinstance(part, dict) or set(part) != {"text"}
                or not isinstance(part["text"], str) for part in parts):
            raise A2AError("only text parts are supported")
        task = "\n".join(part["text"] for part in parts).strip()
        if not task or len(task) > RUNTIME["limits"]["message_chars"]:
            raise A2AError("task is empty or too long")
        configuration = params.get("configuration", {})
        if not isinstance(configuration, dict) or configuration.get("blocking", False) is not False:
            raise A2AError("use nonblocking submission and GetTask polling")
        fingerprint = hashlib.sha256((role + "\n" + task).encode()).hexdigest()
        with self.lock:
            self._authorized(grant.token, role)
            # Persisted message IDs survive API restarts; never repeat actor side effects.
            for saved in self.store.list_chats():
                meta = (saved.get("roleConfig") or {}).get("_ax_delegation", {})
                if meta.get("parent_session_id") == grant.session_id \
                        and meta.get("message_id") == message_id:
                    if meta.get("fingerprint") != fingerprint:
                        raise A2AError("messageId was already used with different input")
                    return self._task(saved)
            self._authorized(grant.token, role)
            if role in grant.lineage:
                raise A2AError("delegation cycle is not allowed")
            if len(grant.lineage) > self.policy["max_depth"]:
                raise A2AError("delegation depth limit reached")
            if grant.run.calls >= self.policy["max_calls_per_turn"]:
                raise A2AError("delegation call limit reached")
            if not self.slots.acquire(blocking=False):
                raise A2AError("delegation is busy; child capacity is full")
            session_id = uuid.uuid4().hex[:16]
            config = {**self.roles[role], "_ax_delegation": {
                "parent_session_id": grant.session_id, "root_session_id": grant.root_id,
                "lineage": [*grant.lineage, role], "message_id": message_id,
                "fingerprint": fingerprint, "input": task, "state": "TASK_STATE_SUBMITTED"}}
            job = Job(session_id, config, task, grant.run, grant.deadline)
            try:
                self._save(job, "TASK_STATE_SUBMITTED")
                self.jobs[session_id] = job
                grant.run.calls += 1
                job.thread = threading.Thread(target=self._execute, args=(job,), daemon=True)
                job.thread.start()
            except Exception:
                self.jobs.pop(session_id, None)
                self.slots.release()
                raise
            return self._task(self.store.get(session_id))

    @staticmethod
    def _remaining(job: Job) -> float:
        remaining = job.deadline - time.monotonic()
        if job.canceled.is_set() or job.run.canceled.is_set() or remaining <= 0:
            raise TimeoutError("delegation canceled or deadline exceeded")
        return remaining

    def _save(self, job: Job, state: str, answer: str | None = None, error: str | None = None,
              failure: dict | None = None):
        with job.lock:
            if job.canceled.is_set() or job.run.canceled.is_set():
                state, answer, error = "TASK_STATE_CANCELED", None, "delegation canceled"
                failure = None
            meta = job.config["_ax_delegation"]
            meta["state"] = state
            for key, value in (("error", error), ("failure", failure), ("answer", answer)):
                if value is not None:
                    meta[key] = value
                else:
                    meta.pop(key, None)
            messages = []
            if answer is not None:
                messages = [{"role": "user", "content": job.task},
                            {"role": "assistant", "content": answer}]
            self.store.save(job.session_id, job.config["agent"], messages,
                            job.config["_ax_role"], job.config)
            print(json.dumps({"event": "ax_delegation", "session_id": job.session_id,
                              "parent_session_id": meta["parent_session_id"],
                              "role": job.config["_ax_role"], "state": state,
                              **({"failure": failure, "reason": error} if failure else {})}),
                  file=sys.stderr, flush=True)

    def _execute(self, job: Job):
        answer, issue, failure = None, None, None
        state = "TASK_STATE_FAILED"
        stage = "worker_startup"
        try:
            self._remaining(job)
            self._save(job, "TASK_STATE_WORKING")
            job.chat = self.runner.start_chat(job.session_id, job.config,
                                               deadline=job.deadline, cancel_event=job.canceled)
            stage = "worker_message"
            answer = job.chat.send(job.task, timeout_seconds=self._remaining(job))["answer"]
            self._remaining(job)
            state = "TASK_STATE_COMPLETED"
        except TimeoutError:
            state, issue = "TASK_STATE_CANCELED", "delegation canceled or deadline exceeded"
        except Exception as exc:
            issue, failure = _worker_failure(exc, stage)
        finally:
            try:
                if job.chat:
                    job.chat.close()
                if job.canceled.is_set() or job.run.canceled.is_set():
                    state, answer, issue = "TASK_STATE_CANCELED", None, "delegation canceled"
                self._save(job, state, answer if state == "TASK_STATE_COMPLETED" else None,
                           issue, failure)
            finally:
                self.revoke(job.session_id)
                with self.lock:
                    self.jobs.pop(job.session_id, None)
                self.slots.release()

    @staticmethod
    def _task(saved: dict) -> dict:
        meta = saved["roleConfig"]["_ax_delegation"]
        task = {"id": saved["sessionId"], "contextId": meta["root_session_id"],
                "status": {"state": meta["state"], "timestamp": saved["updatedAt"]},
                "metadata": {"sessionId": saved["sessionId"], "role": saved["role"],
                             "parentSessionId": meta["parent_session_id"]}}
        if meta.get("error"):
            task["status"]["message"] = {"messageId": saved["sessionId"] + "-error",
                "role": "ROLE_AGENT", "parts": [{"text": meta["error"]}]}
        if meta.get("failure"):
            task["metadata"]["failure"] = meta["failure"]
        if meta["state"] == "TASK_STATE_COMPLETED":
            task["artifacts"] = [{"artifactId": saved["sessionId"] + "-answer", "name": "answer",
                                  "parts": [{"text": meta["answer"]}]}]
        return task

    def close(self):
        with self.lock:
            self.closed = True
            jobs = list(self.jobs.values())
        for job in jobs:
            self._cancel_run(job.run)
        for job in jobs:
            if job.thread:
                job.thread.join(timeout=5)
        with self.lock:
            self.grants.clear()
            self.tokens.clear()


def start_a2a_server(service: DelegationService, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, body):
            encoded = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            match = re.fullmatch(r"/a2a/([a-z][a-z0-9_]*)/\.well-known/agent-card\.json", self.path)
            if not match or match[1] not in service.roles:
                self.reply(404, {})
                return
            self.reply(200, service.card(match[1]))

        def do_POST(self):
            match = re.fullmatch(r"/a2a/([a-z][a-z0-9_]*)", self.path)
            if not match:
                self.reply(404, {})
                return
            authorization = self.headers.get("Authorization", "")
            token = authorization[7:] if authorization.startswith("Bearer ") else ""
            try:
                service._authorized(token, match[1])
            except A2AError:
                self.reply(403, {})
                return
            request_id = None
            try:
                self.connection.settimeout(5)
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= RUNTIME["limits"]["request_bytes"]:
                    raise A2AError("invalid request size", -32600)
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" \
                        or type(body.get("id")) not in {str, int}:
                    raise A2AError("JSON-RPC request with id is required", -32600)
                request_id = body["id"]
                if self.headers.get("A2A-Version") != "1.0":
                    raise A2AError("only A2A-Version 1.0 is supported", -32009)
                result = service.dispatch(token, match[1], body.get("method"), body.get("params", {}))
                self.reply(200, {"jsonrpc": "2.0", "id": request_id, "result": result})
            except A2AError as exc:
                self.reply(200, {"jsonrpc": "2.0", "id": request_id,
                                 "error": {"code": exc.code, "message": str(exc)}})
            except (ValueError, TypeError):
                self.reply(200, {"jsonrpc": "2.0", "id": request_id,
                                 "error": {"code": -32700, "message": "invalid JSON request"}})
            except OSError:
                self.reply(200, {"jsonrpc": "2.0", "id": request_id,
                                 "error": {"code": -32603, "message": "delegation storage unavailable"}})

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server

"""AX delegation policy and execution with per-actor authority.

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
from collections import deque
from dataclasses import dataclass, field

from popot_agents.runtime_config import RUNTIME
from popot_agents.orchestrator.main import AgentConfigurationError, AgentRunError
from popot_agents.progress import emit_progress, observe_progress, scoped_progress

ACTIVE = {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}


def _json_bytes(value) -> int:
    # ASCII escaping is a conservative bound for the SDK's UTF-8 JSON, including
    # the second layer of escaping around native events stored as strings.
    return len(json.dumps(value, ensure_ascii=True).encode('ascii'))


def _activity_budget() -> int:
    return min(65536, RUNTIME['limits']['history_bytes'] // 4)


def _bounded_progress(entry: dict, limit: int) -> dict | None:
    original_bytes = _json_bytes(entry)
    if original_bytes <= limit:
        return entry
    message = entry['message']
    native = json.loads(message)
    # Keep native event attribution and a raw preview; never synthesize a summary
    # that could be mistaken for harness output. Truncation is explicit.
    preview = {'event': native['event'][:128], 'truncated': True,
               'original_bytes': original_bytes, 'preview': ''}
    if isinstance(native.get('agent_role'), str):
        preview['agent_role'] = native['agent_role'][:128]

    def truncated(length):
        preview['preview'] = message[:length]
        return {'seq': entry['seq'], 'message': json.dumps(preview, ensure_ascii=False)}

    if _json_bytes(truncated(0)) > limit:
        return None
    low, high = 0, min(len(message), limit)
    while low < high:
        middle = (low + high + 1) // 2
        if _json_bytes(truncated(middle)) <= limit:
            low = middle
        else:
            high = middle - 1
    return truncated(low)


def _bounded_activity(activity, budget: int) -> list[dict]:
    retained, size = [], 2  # JSON list brackets.
    for entry in reversed(list(activity)[-64:]):
        entry = _bounded_progress(entry, min(16384, max(0, budget - 2)))
        if entry is None:
            continue
        entry_size = _json_bytes(entry) + (2 if retained else 0)
        if size + entry_size > budget:
            break
        retained.append(entry)
        size += entry_size
    return list(reversed(retained))


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
    probe_token: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    probes: int = 0
    config: dict = field(default_factory=dict, repr=False)
    approved_change: str = ''
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
    activity: deque = field(default_factory=lambda: deque(maxlen=64))
    activity_seq: int = 0


class DelegationService:
    def __init__(self, roles: dict, policy: dict, runners: dict, store, base_url: str):
        self.roles, self.policy = roles, policy
        self.runners, self.store = runners, store
        self.base_url = base_url.rstrip("/")
        self.lock = threading.RLock()
        self.grants: dict[str, Grant] = {}
        self.tokens: dict[str, Grant] = {}
        self.jobs: dict[str, Job] = {}
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
                # Task deletion is independent of the old provider, which may
                # no longer be configured after a restart.
                next(iter(self.runners.values())).close_session(saved["sessionId"])
                meta.update(state="TASK_STATE_FAILED", error="AX API restarted during delegation")
                self.store.save(saved["sessionId"], saved["agent"], saved["messages"],
                                saved["role"], config)

    def prepare_config(self, session_id: str, config: dict | None) -> dict:
        config = dict(config or {})
        if config.get('_ax_harness2_probe'):
            return config
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
        if config.get('_ax_harness2_probe'):
            return {}
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
                              run=job.run if job else None, config=json.loads(json.dumps(config)))
                self.grants[session_id] = grant
                self.tokens[hashlib.sha256(grant.token.encode()).hexdigest()] = grant
                self.tokens[hashlib.sha256(grant.probe_token.encode()).hexdigest()] = grant
            environment = {}
            if role == 'project_operator' and getattr(self, 'operations', None):
                environment = {'AX_DELEGATION_URL': self.base_url, 'AX_DELEGATION_TOKEN': grant.token,
                               'AX_DELEGATION_OPERATOR': '1'}
            settings = self.roles[role].get('harness2', {})
            if settings.get('enabled') and settings.get('mode') == 'execution' \
                    and config.get('harness2', {}).get('enabled') \
                    and config.get('harness2', {}).get('mode') == 'execution':
                environment = {'AX_HARNESS2_URL': self.base_url, 'AX_HARNESS2_TOKEN': grant.probe_token,
                               'AX_HARNESS2_ROLE': role}
            allowed = [target for target in self.roles[role].get("allowed_roles", [])
                       if target not in grant.lineage]
            if len(grant.lineage) > self.policy["max_depth"]:
                allowed = []
            if not allowed:
                return environment
            return {**environment, "AX_DELEGATION_URL": self.base_url, "AX_DELEGATION_TOKEN": grant.token,
                    "AX_DELEGATION_ROLES": json.dumps(allowed),
                    "AX_DELEGATION_DESCRIPTIONS": json.dumps({target:
                        self.roles[target].get("description", self.roles[target]["instructions"][:1000])
                        for target in allowed}, ensure_ascii=False)}

    def revoke(self, session_id: str) -> None:
        with self.lock:
            grant = self.grants.pop(session_id, None)
            if grant:
                self.tokens.pop(hashlib.sha256(grant.token.encode()).hexdigest(), None)
                self.tokens.pop(hashlib.sha256(grant.probe_token.encode()).hexdigest(), None)

    @contextmanager
    def turn(self, session_id: str, timeout_seconds: float, *, message: str = ''):
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
                grant.approved_change = (message.strip().split()[1] if
                    re.fullmatch(r'/apply change-[0-9a-f]{24}', message.strip())
                    and not grant.config.get('_ax_delegation') else '')
                grant.probes = 0
                grant.deadline = min(grant.run.deadline, time.monotonic() + timeout_seconds)
                run = grant.run
        try:
            yield
        finally:
            if not yield_without_grant:
                with self.lock:
                    grant.active = False
                    grant.approved_change = ''
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

    def authorized_operator(self, token: str) -> Grant:
        with self.lock:
            grant = self.tokens.get(hashlib.sha256(token.encode()).hexdigest())
            if self.closed or not grant or grant.role != 'project_operator' \
                    or not secrets.compare_digest(token, grant.token) or not grant.active \
                    or not grant.run or grant.run.canceled.is_set() or time.monotonic() >= grant.deadline \
                    or not getattr(self, 'operations', None):
                raise A2AError('operator capability is expired or invalid')
            return grant

    def _authorized(self, token: str, role: str) -> Grant:
        with self.lock:
            grant = self.tokens.get(hashlib.sha256(token.encode()).hexdigest())
            if self.closed or not grant or not grant.active or not grant.run \
                    or grant.run.canceled.is_set() or time.monotonic() >= grant.deadline:
                raise A2AError("delegation capability is expired or invalid")
            if secrets.compare_digest(token, grant.probe_token):
                settings = self.roles[grant.role].get('harness2', {})
                if role != grant.role or not settings.get('enabled') or settings.get('mode') != 'execution':
                    raise A2AError('probe capability is restricted to its configured role')
            elif role not in self.roles[grant.role].get("allowed_roles", []):
                raise A2AError("target role is not allowed")
            return grant

    def card(self, role: str) -> dict:
        if role not in self.roles:
            raise A2AError("unknown role")
        return {"name": f"popot AX {role}", "description": self.roles[role].get(
                    "description", self.roles[role]["instructions"]),
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
            return {"task": self._submit(grant, role, params, token=token)}
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

    def _submit(self, grant: Grant, role: str, params: dict, *, token: str) -> dict:
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
        if not isinstance(configuration, dict):
            raise A2AError("configuration must be an object")
        metadata = message.get('metadata', {})
        if not isinstance(metadata, dict):
            raise A2AError('message metadata must be an object')
        probe = metadata.get('harness2')
        # Only the dedicated probe capability can enter this path; a normal
        # delegation request cannot opt into a more privileged same-role actor.
        is_probe = secrets.compare_digest(token, grant.probe_token)
        if is_probe:
            if not isinstance(probe, dict) or set(probe) != {'prompt', 'timeout_seconds'} \
                    or not isinstance(probe.get('prompt'), str) or len(probe['prompt']) > 1500 \
                    or type(probe.get('timeout_seconds')) not in {int, float} \
                    or not 0 < probe['timeout_seconds'] <= 300:
                raise A2AError('invalid Harness2 probe')
        elif probe is not None:
            raise A2AError('probe must target the same role')
        fingerprint = hashlib.sha256((role + '\n' + task +
            (json.dumps(probe, sort_keys=True) if is_probe else '')).encode()).hexdigest()
        with self.lock:
            self._authorized(token, role)
            # Persisted message IDs survive API restarts; never repeat actor side effects.
            for saved in self.store.list_chats():
                meta = (saved.get("roleConfig") or {}).get("_ax_delegation", {})
                if meta.get("parent_session_id") == grant.session_id \
                        and meta.get("message_id") == message_id:
                    if meta.get("fingerprint") != fingerprint:
                        raise A2AError("messageId was already used with different input")
                    return self._task(saved)
            self._authorized(token, role)
            if role in grant.lineage and not is_probe:
                raise A2AError("delegation cycle is not allowed")
            if not is_probe and len(grant.lineage) > self.policy["max_depth"]:
                raise A2AError("delegation depth limit reached")
            if is_probe and grant.probes >= 4:
                raise A2AError('Harness2 probe limit reached')
            if is_probe and any(job.config.get('_ax_harness2_probe') and
                    job.config['_ax_delegation']['state'] in ACTIVE and
                    job.config['_ax_delegation']['parent_session_id'] == grant.session_id
                    for job in self.jobs.values()):
                raise A2AError('a Harness2 probe is already active')
            queue = getattr(self.runners[self.roles[role]['agent']], 'worker_queue', None)
            if is_probe and queue:
                capacity = queue.status()
                if capacity['active'] >= capacity['limit'] or capacity['queued']:
                    raise A2AError('No spare worker capacity for a Harness2 probe')
            if not is_probe and grant.run.calls >= self.policy["max_calls_per_turn"]:
                raise A2AError("delegation call limit reached")
            session_id = uuid.uuid4().hex[:16]
            config = {**self.roles[role], "_ax_delegation": {
                "parent_session_id": grant.session_id, "root_session_id": grant.root_id,
                "lineage": [*grant.lineage, role], "message_id": message_id,
                "fingerprint": fingerprint, "input": task, "state": "TASK_STATE_SUBMITTED"}}
            deadline = grant.deadline
            if is_probe:
                parent = {**grant.config, '_ax_delegation': config['_ax_delegation']}
                parent['tools'] = [name for name in parent.get('tools', [])
                                   if name in self.roles[role].get('tools', [])]
                config = self._probe_config(parent, probe, self.roles[role])
                deadline = min(deadline, time.monotonic() + probe['timeout_seconds'])
            job = Job(session_id, config, task, grant.run, deadline)
            try:
                self._save(job, "TASK_STATE_SUBMITTED")
                self.jobs[session_id] = job
                if is_probe:
                    grant.probes += 1
                else:
                    grant.run.calls += 1
                job.thread = threading.Thread(target=self._execute, args=(job,), daemon=True)
                job.thread.start()
            except Exception:
                self.jobs.pop(session_id, None)
                raise
            return self._task(self.store.get(session_id))

    @staticmethod
    def _probe_config(config, probe, current):
        """Restrict MCP to reads; local changes live in this AX Task."""
        readonly = {'get_file_contents', 'get_commit', 'search_repositories',
                    'list_branches', 'pull_request_read', 'issue_read'}
        servers = {}
        github = config.get('mcpServers', {}).get('github')
        current_github = current.get('mcpServers', {}).get('github', {})
        if github and current_github and github.get('url') in {'https://api.githubcopilot.com/mcp/',
                                          'https://api.githubcopilot.com/mcp/x/all/readonly'}:
            servers['github'] = {**github, 'tools': [name for name in github['tools']
                if name in readonly and name in current_github.get('tools', [])],
                'url': 'https://api.githubcopilot.com/mcp/x/all/readonly',
                'bearer_token_env': 'AX_HARNESS2_GITHUB_TOKEN'}
        return {**config, '_ax_harness2_probe': True, 'harness2': {'enabled': False},
            'allowed_roles': [], 'env_names': [], 'mcpServers': servers,
            'timeout_seconds': min(300, max(1, int(probe['timeout_seconds']))), 'max_tool_rounds': 12,
            'instructions': config['instructions'] + '\n\nIsolated Harness2 probe: perform the task '
                'using actual tools and report evidence. Local edits and checks are allowed. '
                'External publication, delegation and memory writes are unavailable. '
                'Other workers\' files are unavailable; use supplied source or accessible repository refs. '
                'Return the diff and exact check results when you change code.\n' + probe['prompt']}

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
            meta['activity'] = list(job.activity)
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

    def _record_progress(self, job: Job, message: str):
        message = scoped_progress(job.config['_ax_role'], message)
        if message is None:
            return
        with job.lock:
            if job.canceled.is_set() or job.run.canceled.is_set():
                return
            job.activity_seq += 1
            job.activity = deque(_bounded_activity(
                [*job.activity, {'seq': job.activity_seq, 'message': message}],
                _activity_budget()), maxlen=64)

    def _execute(self, job: Job):
        # Each child owns its observer; parent clients read only their authorized
        # Task's bounded activity through the existing A2A polling transport.
        with observe_progress(lambda message: self._record_progress(job, message)):
            self._execute_observed(job)

    def _execute_observed(self, job: Job):
        answer, issue, failure = None, None, None
        state = "TASK_STATE_FAILED"
        stage = "worker_startup"
        try:
            self._remaining(job)
            self._save(job, "TASK_STATE_WORKING")
            runner = self.runners[job.config['agent']]
            job.chat = runner.start_chat(job.session_id, job.config,
                                         deadline=job.deadline, cancel_event=job.canceled)
            stage = "worker_message"
            emit_progress('turn_started')
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
                emit_progress('completed' if state == 'TASK_STATE_COMPLETED' else 'failed')
                self._save(job, state, answer if state == "TASK_STATE_COMPLETED" else None,
                           issue, failure)
            finally:
                self.revoke(job.session_id)
                with self.lock:
                    self.jobs.pop(job.session_id, None)

    def _task(self, saved: dict) -> dict:
        meta = saved["roleConfig"]["_ax_delegation"]
        task = {"id": saved["sessionId"], "contextId": meta["root_session_id"],
                "status": {"state": meta["state"], "timestamp": saved["updatedAt"]},
                "metadata": {"sessionId": saved["sessionId"], "role": saved["role"],
                             "parentSessionId": meta["parent_session_id"]}}
        activity = meta.get('activity', [])
        with self.lock:
            job = self.jobs.get(saved['sessionId'])
        if job:
            with job.lock:
                activity = list(job.activity)
        if meta.get("error"):
            task["status"]["message"] = {"messageId": saved["sessionId"] + "-error",
                "role": "ROLE_AGENT", "parts": [{"text": meta["error"]}]}
        if meta.get("failure"):
            task["metadata"]["failure"] = meta["failure"]
        if meta["state"] == "TASK_STATE_COMPLETED":
            task["artifacts"] = [{"artifactId": saved["sessionId"] + "-answer", "name": "answer",
                                  "parts": [{"text": meta["answer"]}]}]
        # Result/status takes precedence, including for old persisted Tasks whose
        # progress predates these limits. The transport's extra 4096 bytes remain
        # reserved for the SDK envelope and protobuf metadata number formatting.
        budget = min(_activity_budget(), max(0,
            RUNTIME['limits']['history_bytes'] - _json_bytes(task) - 64))
        activity = _bounded_activity(activity, budget)
        if activity:
            task['metadata']['activity'] = activity
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


def start_a2a_server(service: DelegationService, host: str, port: int):
    from .a2a_sdk import A2AServer
    return A2AServer(service, host, port)

"""AX delegation policy and execution with per-actor authority.

Child sessions use the existing session store and AX runner. No provider keys
or delegation capabilities are persisted in transcripts or role configuration.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from collections import deque
from dataclasses import dataclass, field

from popot_agents.runtime_config import RUNTIME
from popot_agents.work_evidence import evidence_event
from ax_local.config import AX_CONFIG
from popot_agents.orchestrator.workflow import WorkflowRun
from popot_agents.workflow_config import role_policy
from popot_agents.orchestrator.main import AgentConfigurationError, AgentRunError
from popot_agents.progress import emit_progress, observe_progress, scoped_progress, forward_progress

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
    if isinstance(native.get('agent_session_id'), str):
        preview['agent_session_id'] = native['agent_session_id'][:128]

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
        elif 'ActorResumeFailed' in detail:
            code, reason = 'actor_resume_failed', 'AX actor could not start; inspect physical worker capacity and controller events'
        elif detail == 'AX task lookup failed':
            code, reason = 'ax_task_lookup_failed', 'AX task lookup failed; inspect AX API and Kubernetes control plane'
        elif detail in {"model endpoint is unreachable", "model endpoint returned no answer",
                        "model endpoint returned an empty answer"}:
            code, reason = "model_response_failed", detail
    return reason, {"stage": stage, "code": code, "error_type": type(exc).__name__}


class A2AError(ValueError):
    def __init__(self, message: str, code: int = -32602):
        super().__init__(message)
        self.code = code


class DependencyBlocked(AgentRunError):
    pass


@dataclass
class Run:
    deadline: float
    canceled: threading.Event = field(default_factory=threading.Event)
    calls: int = 0
    persistent: bool = False
    call_limit: int = 0
    turn_calls: int = 0
    metadata: dict = field(default_factory=dict, repr=False)
    max_turns: int = 0
    root_id: str = ''
    pool: object = field(default=None, repr=False)
    reservation: str | None = None
    reserved_slots: int = 0
    admitted: dict = field(default_factory=dict, repr=False)
    lane_condition: object = field(default_factory=threading.Condition, repr=False)


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
    messages: list = field(default_factory=list)
    waiting_for: str | None = None


class DelegationService:
    def __init__(self, roles: dict, policy: dict, runners: dict, store, base_url: str):
        self.roles, self.policy = roles, policy
        self.runners, self.store = runners, store
        self.base_url = base_url.rstrip("/")
        self.lock = threading.RLock()
        self.grants: dict[str, Grant] = {}
        self.tokens: dict[str, Grant] = {}
        self.jobs: dict[str, Job] = {}
        self.retained: dict[str, tuple[object, float]] = {}
        self.closed = False
        self.discovery_samples = {}

    @contextmanager
    def workflow(self, session_id, deadline, *, max_turns, metadata=None):
        with self.lock:
            grant = self.grants.get(session_id)
            if grant is None:
                yield_run = None
            else:
                if grant.run is not None:
                    raise RuntimeError('workflow is already active')
                metadata = metadata if metadata is not None else {}
                metadata.setdefault('publication_owner',session_id if grant.role in {'backend_engineer','frontend_engineer'} else None)
                yield_run = Run(deadline, persistent=True, calls=metadata.get('delegationCalls', 0),
                                metadata=metadata, max_turns=max_turns, root_id=session_id,
                                call_limit=self.policy['max_calls_per_turn'] * max_turns)
                pool = getattr(self.runners[grant.config['agent']], 'worker_queue', None)
                if pool is not None:
                    yield_run.pool = pool
                    yield_run.reservation = metadata.get('run_id') or secrets.token_hex(16)
                grant.run = yield_run
        try:
            if yield_run and yield_run.pool is not None:
                yield_run.reserved_slots = yield_run.pool.reserve_full(
                    yield_run.reservation, self.policy['max_depth'], deadline=deadline)
            yield yield_run
        finally:
            if yield_run:
                self._cancel_run(yield_run)
                if yield_run.pool is not None:
                    yield_run.pool.release_reservation(yield_run.reservation)
                with self.lock:
                    if grant.run is yield_run:
                        grant.run = None

    def turn_context(self, session_id):
        with self.lock:
            grant = self.grants.get(session_id)
            if not grant or not grant.run or not grant.run.persistent:
                return None
            run = grant.run
            job = self.jobs.get(session_id)
            meta = job.config['_ax_delegation'] if job else {}
            turns = meta.get('workflow', {}).get('turns', 0) if job else run.metadata.get('turns', 0)
            limit = role_policy(job.config)['max_turns'] if job else run.max_turns
            return {'run_id':run.metadata.get('run_id'), 'root_session_id':run.root_id,
                'session_id':session_id,'role':grant.role,
                'publication_owner':run.metadata.get('publication_owner'),
                'authorization':run.metadata.get('authorization', [run.metadata.get('original_task','')]),
                'assignments':meta.get('scope_assignments', []),
                'target_revision':meta.get('revision'), 'can_delegate':turns < limit,
                'dependency_results':meta.get('dependency_results',[])}

    def wait_state(self, session_id):
        with self.lock:
            job = self.jobs.get(session_id)
            return job.waiting_for if job else None

    def setup_estimate(self, config):
        role = config.get('_ax_role')
        with self.lock:
            return sum(max(self.discovery_samples.get((role, alias),
                [RUNTIME['timeouts']['mcp_discovery_seconds']])) * 1.5 + 1
                for alias in config.get('mcpServers', {}) if alias != 'cognee')

    def await_children(self, parent_id, worker, history, deadline):
        """Wait outside a model turn, then reconcile only this parent's receipts."""
        checkpoint = worker.get_checkpoint() if hasattr(worker, 'get_checkpoint') else {}
        pending = {value['session_id'] for _, value in checkpoint.get('failed_delegations', [])
                   if value.get('pending') and value.get('session_id')}
        with self.lock:
            pending.update(sid for sid, job in self.jobs.items()
                if job.run.persistent and job.config['_ax_delegation']['parent_session_id'] == parent_id)
        selection = checkpoint.get('await_tasks')
        mode = 'all'
        if selection is not None:
            ids = selection.get('session_ids') if isinstance(selection,dict) else None
            mode = selection.get('wait_for','all') if isinstance(selection,dict) else None
            if not isinstance(ids,list) or not 1 <= len(ids) <= 16 or any(
                    not isinstance(sid,str) or not re.fullmatch(r'[0-9a-f]{16}',sid) for sid in ids) \
                    or len(set(ids)) != len(ids) or mode not in {'all','any'}:
                raise AgentRunError('Invalid child wait selection')
            pending = set(ids)
        for sid in pending:
            saved = self.store.get(sid)
            if not saved or (saved.get('roleConfig') or {}).get('_ax_delegation',{}).get('parent_session_id') != parent_id:
                raise AgentRunError('Child wait ownership mismatch')
        if not pending:
            return False
        emit_progress('waiting_for_children', child_count=len(pending))
        if getattr(getattr(worker, 'runner', None), 'worker_pool', None) is not None:
            worker.after_turn()
        seen = {}
        while True:
            remaining = deadline - time.monotonic()
            parent_job = self.jobs.get(parent_id)
            grant = self.grants.get(parent_id)
            run = parent_job.run if parent_job else grant.run if grant else None
            if self.closed or remaining <= 0 or run and run.canceled.is_set() or parent_job and parent_job.canceled.is_set():
                raise TimeoutError('workflow canceled or deadline exceeded while waiting for children')
            with self.lock:
                jobs = [self.jobs[sid] for sid in pending if sid in self.jobs]
                done = pending - {job.session_id for job in jobs}
            blocked = any(((self.store.get(sid) or {}).get('roleConfig') or {}).get(
                '_ax_delegation',{}).get('outcome',{}).get('status') != 'completed' for sid in done)
            if done and (mode == 'any' or done == pending or blocked):
                pending = done
                break
            for job in jobs:
                with job.lock:
                    activity = list(job.activity)
                for event in activity:
                    if event['seq'] > seen.get(job.session_id, 0):
                        forward_progress(event['message'])
                        seen[job.session_id] = event['seq']
            if run:
                with run.lane_condition:run.lane_condition.wait(min(.1,remaining))
            else:
                time.sleep(min(.1,remaining))
        receipts = {}
        for sid in pending:
            saved = self.store.get(sid)
            meta = (saved.get('roleConfig') or {}).get('_ax_delegation', {}) if saved else {}
            if meta.get('parent_session_id') != parent_id or meta.get('state') in ACTIVE:
                raise AgentRunError('Pending child ownership/state requires reconciliation')
            receipt = {'sessionId':sid, 'role':saved['role'], 'state':meta['state'],
                       'outcome':meta.get('outcome', {'status':'failed','reason':'missing outcome'}),
                       'resumable':bool(meta.get('resumable') and sid in self.retained),
                       'answer':meta.get('answer',''), 'truncated':False}
            if meta.get('outcomeRecordedAt'):
                receipt['reportedAt'] = meta['outcomeRecordedAt']
            if meta.get('error'):
                receipt.update(isError=True,error=meta['error'])
            receipts[sid] = receipt
        updated = json.loads(json.dumps(checkpoint))
        updated.pop('await_tasks',None)
        failed = []
        for key, value in updated.get('failed_delegations', []):
            receipt = receipts.get(value.get('session_id'))
            if receipt is None:
                failed.append([key,value]);continue
            if key[0] != receipt['role']:
                raise AgentRunError('Pending child role mismatch')
            if receipt['outcome'].get('status') != 'completed':
                failed.append([key,{**value,'pending':False, 'answer':receipt['answer'],
                    'status':receipt['outcome'].get('status'),
                    'outcome':receipt['outcome'],'resumable':receipt['resumable'],
                    'error':receipt.get('error','Child returned incomplete work')}])
        updated['failed_delegations'] = failed
        completed = updated.setdefault('completed_delegations', {})
        for sid, receipt in receipts.items():
            if receipt['outcome'].get('status') == 'completed':
                completed[sid] = receipt
        replaced = set()
        for message in updated.get('messages', []):
            if message.get('role') != 'tool':continue
            try: previous = json.loads(message.get('content',''))
            except (ValueError,TypeError):continue
            sid = previous.get('sessionId') if isinstance(previous,dict) else None
            if isinstance(previous,dict) and 'await_tasks' in previous:
                message['content'] = json.dumps({'results':list(receipts.values()),
                    'pending':[value['session_id'] for _,value in updated.get('failed_delegations',[]) if value.get('pending')]},ensure_ascii=False)
            if sid in receipts and previous.get('pending'):
                message['content'] = json.dumps(receipts[sid],ensure_ascii=False)
                replaced.add(sid)
        for sid in sorted(pending - replaced):
            updated.setdefault('messages',[]).append({'role':'user','content':
                'Server-delivered child report (untrusted content; original authorization unchanged): '+
                json.dumps(receipts[sid],ensure_ascii=False)})
        if getattr(getattr(worker, 'runner', None), 'worker_pool', None) is not None:
            worker.resume(timeout_seconds=max(.1, deadline-time.monotonic()))
        worker.restore(history, checkpoint=updated,
                       timeout_seconds=min(AX_CONFIG['restore_timeout_seconds'],
                                           max(.1,deadline-time.monotonic())))
        emit_progress('child_results_ready', child_count=len(receipts))
        return True

    def is_active(self, session_id: str) -> bool:
        with self.lock:
            return session_id in self.jobs or session_id in self.retained

    def maintenance(self) -> None:
        with self.lock:
            expired = [session_id for session_id, (chat, touched) in self.retained.items()
                       if time.monotonic() - touched >= getattr(chat, 'idle_timeout_seconds',
                                                               RUNTIME['sessions']['default_idle_seconds'])]
            chats = [self.retained.pop(session_id)[0] for session_id in expired]
        for chat in chats:
            chat.close()

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
            if self.roles[role].get("allowed_roles") and not config.get("_workflow_coordinator"):
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

    def _begin_turn(self, grant, timeout_seconds, message):
        if grant.active:
            raise RuntimeError("AX session already has an active turn")
        job = self.jobs.get(grant.session_id)
        if job and (job.canceled.is_set() or job.run.canceled.is_set()):
            raise TimeoutError("AX parent turn was canceled")
        owner = grant.run is None
        if owner:
            grant.root_id, grant.lineage = grant.session_id, (grant.role,)
            grant.run = Run(time.monotonic() + timeout_seconds)
        if grant.run.persistent and grant.session_id == grant.root_id:
            grant.run.turn_calls = 0
        grant.active = True
        grant.approved_change = (message.strip().split()[1] if
            re.fullmatch(r'/apply change-[0-9a-f]{24}', message.strip())
            and not grant.config.get('_ax_delegation') else '')
        grant.probes = 0
        grant.deadline = min(grant.run.deadline, time.monotonic() + timeout_seconds)
        return owner

    @contextmanager
    def turn(self, session_id: str, timeout_seconds: float, *, message: str = ''):
        with self.lock:
            grant = self.grants.get(session_id)
            owner = False
            if grant is not None:
                owner = self._begin_turn(grant, timeout_seconds, message)
                run = grant.run
        try:
            yield
        finally:
            if grant is not None:
                with self.lock:
                    grant.active = False
                    grant.approved_change = ''
                    if owner:
                        grant.run = None
                if not run.persistent:
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

    @staticmethod
    def _message_input(message, root_id):
        if not isinstance(message, dict) or message.get("role") != "ROLE_USER":
            raise A2AError("message with ROLE_USER is required")
        message_id = message.get("messageId")
        if not isinstance(message_id, str) or not 1 <= len(message_id) <= 100:
            raise A2AError("messageId must be a nonempty string up to 100 characters")
        session_id = message.get('taskId')
        if session_id is not None and (not isinstance(session_id, str) or
                not re.fullmatch(r'[0-9a-f]{16}', session_id)):
            raise A2AError('invalid continuation session ID')
        if message.get("contextId", root_id) != root_id:
            raise A2AError("task must belong to the caller's context")
        parts = message.get("parts")
        if not isinstance(parts, list) or not parts or any(
                not isinstance(part, dict) or set(part) != {"text"}
                or not isinstance(part["text"], str) for part in parts):
            raise A2AError("only text parts are supported")
        task = "\n".join(part["text"] for part in parts).strip()
        if not task or len(task) > RUNTIME["limits"]["message_chars"]:
            raise A2AError("task is empty or too long")
        return message_id, task, session_id

    @staticmethod
    def _probe_request(grant, message, token):
        metadata = message.get('metadata', {})
        if not isinstance(metadata, dict):
            raise A2AError('message metadata must be an object')
        call_timeout = metadata.get('timeout_seconds')
        if 'timeout_seconds' in metadata and (type(call_timeout) not in {int, float}
                or not math.isfinite(call_timeout) or call_timeout <= 0):
            raise A2AError('timeout_seconds must be positive and finite')
        probe = metadata.get('harness2')
        # Only the dedicated probe capability can enter this path; a normal
        # delegation request cannot opt into a more privileged same-role actor.
        is_probe = secrets.compare_digest(token, grant.probe_token)
        if is_probe and message.get('taskId'):
            raise A2AError('Harness2 probes cannot continue delegated sessions')
        if is_probe:
            if not isinstance(probe, dict) or set(probe) != {'prompt', 'timeout_seconds'} \
                    or not isinstance(probe.get('prompt'), str) or len(probe['prompt']) > 1500 \
                    or type(probe.get('timeout_seconds')) not in {int, float} \
                    or not 0 < probe['timeout_seconds'] <= 300:
                raise A2AError('invalid Harness2 probe')
        elif probe is not None:
            raise A2AError('probe must target the same role')
        return probe, is_probe, call_timeout

    def _existing_submission(self, parent_id, message_id, fingerprint):
        # Persisted message IDs survive API restarts; never repeat actor side effects.
        for saved in self.store.list_chats():
            meta = (saved.get("roleConfig") or {}).get("_ax_delegation", {})
            previous = meta.get('attempts', {}).get(message_id)
            if meta.get("parent_session_id") == parent_id and (
                    previous or meta.get("message_id") == message_id):
                if (previous or meta.get("fingerprint")) != fingerprint:
                    raise A2AError("messageId was already used with different input")
                return self._task(saved)
        return None

    def _check_probe_limits(self, grant, role):
        if grant.probes >= 4:
            raise A2AError('Harness2 probe limit reached')
        if any(job.config.get('_ax_harness2_probe') and
                job.config['_ax_delegation']['state'] in ACTIVE and
                job.config['_ax_delegation']['parent_session_id'] == grant.session_id
                for job in self.jobs.values()):
            raise A2AError('a Harness2 probe is already active')
        queue = getattr(self.runners[self.roles[role]['agent']], 'worker_queue', None)
        if queue:
            capacity = queue.status()
            if capacity['active'] >= capacity['limit'] or capacity['queued']:
                raise A2AError('No spare worker capacity for a Harness2 probe')

    def _check_submission_limits(self, grant, role, is_probe):
        if is_probe:
            self._check_probe_limits(grant, role)
            return
        if role in grant.lineage:
            raise A2AError('delegation cycle is not allowed')
        if len(grant.lineage) > self.policy['max_depth']:
            raise A2AError('delegation depth limit reached')
        if grant.run.persistent:
            job = self.jobs.get(grant.session_id)
            turns = (job.config['_ax_delegation'].get('workflow',{}).get('turns',0) if job
                     else grant.run.metadata.get('turns',0))
            limit = role_policy(job.config)['max_turns'] if job else grant.run.max_turns
            if turns >= limit:
                raise A2AError('The last coordinator turn is reserved for acceptance; no child started')
            if grant.run.calls >= grant.run.call_limit:
                raise A2AError('workflow delegation call limit reached')
            if grant.run.turn_calls >= self.policy['max_calls_per_turn']:
                raise A2AError('delegation call limit reached')
        elif grant.run.calls >= self.policy['max_calls_per_turn']:
            raise A2AError('delegation call limit reached')

    def _submit(self, grant: Grant, role: str, params: dict, *, token: str) -> dict:
        message = params.get('message')
        message_id, task, session_id = self._message_input(message, grant.root_id)
        if not isinstance(params.get('configuration', {}), dict):
            raise A2AError('configuration must be an object')
        probe, is_probe, call_timeout = self._probe_request(grant, message, token)
        extra = message.get('metadata', {}) or {}
        assignment = extra.get('scope_assignment', task)
        revision = extra.get('revision')
        if not isinstance(assignment, str) or len(assignment) > RUNTIME['limits']['message_chars']:
            raise A2AError('invalid delegated assignment')
        if revision is not None and (not isinstance(revision, str) or not re.fullmatch(r'[a-f0-9]{40}',revision)):
            raise A2AError('invalid target revision')
        dependencies = extra.get('depends_on',[])
        if not isinstance(dependencies,list) or len(dependencies)>16 or any(
                not isinstance(sid,str) or not re.fullmatch(r'[0-9a-f]{16}',sid) for sid in dependencies) \
                or len(set(dependencies))!=len(dependencies):
            raise A2AError('depends_on must contain unique direct child session IDs')
        if dependencies and (not grant.run.persistent or is_probe):
            raise A2AError('dependencies require a durable workflow')
        fingerprint = hashlib.sha256((role + '\n' + (session_id or '') + '\n' + task +
            (json.dumps(probe, sort_keys=True) if is_probe else '') + assignment + (revision or '') + json.dumps(sorted(dependencies))).encode()).hexdigest()
        with self.lock:
            self._authorized(token, role)
            existing = self._existing_submission(grant.session_id, message_id, fingerprint)
            if existing is not None:
                return existing
            self._authorized(token, role)
            self._check_submission_limits(grant, role, is_probe)
            for sid in dependencies:
                dependency = self.store.get(sid)
                meta = (dependency.get('roleConfig') or {}).get('_ax_delegation',{}) if dependency else {}
                if not dependency or meta.get('parent_session_id') != grant.session_id or meta.get('run_id') != grant.run.metadata.get('run_id'):
                    raise A2AError('dependencies must belong to this parent and workflow run')
                queue,seen=[sid],set()
                while queue:
                    current=queue.pop()
                    if current==session_id:
                        raise A2AError('dependency cycle is not allowed')
                    if current in seen:continue
                    seen.add(current)
                    saved=self.store.get(current)
                    queue.extend(((saved or {}).get('roleConfig') or {}).get('_ax_delegation',{}).get('depends_on',[]))
            previous_messages, chat = [], None
            if session_id:
                saved = self.store.get(session_id)
                meta = (saved.get('roleConfig') or {}).get('_ax_delegation', {}) if saved else {}
                if not saved or saved['role'] != role or meta.get('parent_session_id') != grant.session_id \
                        or meta.get('root_session_id') != grant.root_id:
                    raise A2AError('continuation task not found', -32001)
                if session_id in self.jobs:
                    raise A2AError('continuation task is already active')
                if meta.get('outcome', {}).get('status') not in {'partial', 'blocked', 'failed'}:
                    raise A2AError('only unfinished checkpointed tasks can continue')
                retained = self.retained.get(session_id)
                if not retained or retained[0].closed:
                    raise A2AError('continuation sandbox is unavailable; refusing to create an empty replacement')
                config = json.loads(json.dumps(saved['roleConfig']))
                previous_messages, chat = saved['messages'], retained[0]
                config['_ax_delegation'].update(message_id=message_id, fingerprint=fingerprint, input=task)
            else:
                session_id = uuid.uuid4().hex[:16]
                config = {**self.roles[role], "_ax_delegation": {
                    "parent_session_id": grant.session_id, "root_session_id": grant.root_id,
                    "lineage": [*grant.lineage, role], "message_id": message_id,
                    "fingerprint": fingerprint, "input": task, "state": "TASK_STATE_SUBMITTED"}}
            config['_ax_delegation'].setdefault('attempts', {})[message_id] = fingerprint
            config['_ax_delegation'].pop('outcome', None)
            config['_ax_delegation']['resumable'] = False
            durable = grant.run.persistent and not is_probe
            config['_ax_delegation']['durable'] = durable
            if grant.run.metadata.get('request_id'):
                config['_ax_delegation']['root_call_id'] = grant.run.metadata['request_id']
            config['_ax_delegation']['depends_on'] = dependencies
            config['_ax_delegation'].pop('dependency_results',None)
            parent_meta = grant.config.get('_ax_delegation', {})
            config['_ax_delegation']['scope_assignments'] = list(dict.fromkeys([
                *parent_meta.get('scope_assignments', []),
                *config['_ax_delegation'].get('scope_assignments', []), assignment]))
            config['_ax_delegation']['run_id'] = grant.run.metadata.get('run_id')
            if durable and grant.run.pool is not None:
                config['_ax_delegation']['pool_reservation'] = grant.run.reservation
            if revision:
                config['_ax_delegation']['revision'] = revision
            deadline = grant.deadline
            if durable:
                parent_job = self.jobs.get(grant.session_id)
                deadline = min(grant.run.deadline, parent_job.deadline if parent_job else grant.run.deadline)
                parent_remaining = max(0, deadline-time.monotonic())
                deadline -= min(RUNTIME['model']['final_answer_reserve_seconds'],
                                parent_remaining / RUNTIME['model']['final_answer_reserve_divisor'])
                prior_deadline = config['_ax_delegation'].get('deadlineAt')
                if prior_deadline is not None:
                    deadline = min(deadline, time.monotonic() + max(0, prior_deadline - time.time()))
                deadline = min(deadline, time.monotonic() + role_policy(config)['total_seconds'])
                remaining = max(0, deadline-time.monotonic())
                estimate = self.setup_estimate(config)
                reserve = min(RUNTIME['model']['final_answer_reserve_seconds'],
                              remaining / RUNTIME['model']['final_answer_reserve_divisor'])
                if remaining <= estimate + reserve:
                    raise A2AError('Insufficient workflow budget for measured MCP setup and final answer')
                config['_ax_delegation']['setupEstimateSeconds'] = estimate
                config['_ax_delegation']['deadlineAt'] = time.time() + max(0, deadline-time.monotonic())
            elif call_timeout is not None:
                deadline = min(deadline, time.monotonic() + call_timeout)
            if is_probe:
                parent = {**grant.config, '_ax_delegation': config['_ax_delegation']}
                parent['tools'] = [name for name in parent.get('tools', [])
                                   if name in self.roles[role].get('tools', [])]
                config = self._probe_config(parent, probe, self.roles[role])
                deadline = min(deadline, time.monotonic() + probe['timeout_seconds'])
            job = Job(session_id, config, task, grant.run, deadline, chat=chat, messages=previous_messages)
            try:
                self._save(job, "TASK_STATE_SUBMITTED")
                self.jobs[session_id] = job
                owner=grant.run.metadata.get('publication_owner')
                if durable and role in {'backend_engineer','frontend_engineer'} and (
                        owner is None or owner != grant.run.root_id and owner not in self.jobs and owner not in self.retained):
                    grant.run.metadata['publication_owner']=session_id
                if chat:
                    if durable and grant.run.pool is not None:
                        grant.run.pool.adopt_reservation(chat.container_name, grant.run.reservation)
                    self.retained.pop(session_id)
                    self.grants[session_id].run = grant.run
                    self.grants[session_id].config = json.loads(json.dumps(config))
                if is_probe:
                    grant.probes += 1
                else:
                    grant.run.calls += 1
                    grant.run.turn_calls += 1
                    if grant.run.persistent:
                        grant.run.metadata['delegationCalls'] = grant.run.calls
                        root = self.store.get(grant.root_id)
                        if root:
                            root_config = {**(root.get('roleConfig') or {}), '_workflow': grant.run.metadata}
                            self.store.save(grant.root_id, root['agent'], root['messages'], root['role'], root_config)
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
                job.config['_ax_delegation'].update(resumable=False,
                    outcome={**job.config['_ax_delegation'].get('outcome',{}),
                             'status':'failed','reason':'delegation canceled'})
            meta = job.config["_ax_delegation"]
            meta["state"] = state
            meta['activity'] = list(job.activity)
            for key, value in (("error", error), ("failure", failure), ("answer", answer)):
                if value is not None:
                    meta[key] = value
                else:
                    meta.pop(key, None)
            messages = list(job.messages)
            if answer is not None:
                messages += [{"role": "user", "content": job.task},
                             {"role": "assistant", "content": answer}]
            self.store.save(job.session_id, job.config["agent"], messages,
                            job.config["_ax_role"], job.config)
            print(json.dumps({"event": "ax_delegation", "session_id": job.session_id,
                              "parent_session_id": meta["parent_session_id"],
                              "role": job.config["_ax_role"], "state": state,
                              **({"failure": failure, "reason": error} if failure else {})}),
                  file=sys.stderr, flush=True)

    def _record_progress(self, job: Job, message: str):
        message = scoped_progress(job.config['_ax_role'], message, session_id=job.session_id)
        if message is None:
            return
        event = json.loads(message)
        elapsed = event.get('elapsed_seconds')
        if event.get('event') == 'mcp_discovery_completed' and type(elapsed) in (int,float) and 0 <= elapsed <= 900:
            with self.lock:
                key = (event.get('agent_role'), event.get('server'))
                self.discovery_samples.setdefault(key, deque(maxlen=8)).append(elapsed)
        with job.lock:
            if job.canceled.is_set() or job.run.canceled.is_set():
                return
            if event.get('agent_session_id') == job.session_id:
                phase = event.get('event')
                if phase == 'model_request': job.waiting_for = 'model'
                elif phase == 'tool_call': job.waiting_for = 'tool'
                elif phase == 'waiting_for_children': job.waiting_for = 'children'
                elif phase == 'child_results_ready': job.waiting_for = None
                elif phase == 'work_evidence' and event.get('receipt',{}).get('kind') == 'ci':
                    job.waiting_for = 'ci' if event['receipt'].get('status') == 'observed' else None
            job.activity_seq += 1
            job.activity = deque(_bounded_activity(
                [*job.activity, {'seq': job.activity_seq, 'message': message}],
                _activity_budget()), maxlen=64)
            if event.get('agent_session_id') == job.session_id and (receipt := evidence_event(message)):
                self.store.record_evidence(job.session_id, receipt)

    def _execute(self, job: Job):
        # Each child owns its observer; parent clients read only their authorized
        # Task's bounded activity through the existing A2A polling transport.
        with observe_progress(lambda message: self._record_progress(job, message)):
            self._execute_observed(job)

    def _execute_observed(self, job: Job):
        answer, issue, failure = None, None, None
        workflow = None
        state = "TASK_STATE_FAILED"
        stage = "worker_startup"
        try:
            if job.run.persistent and not job.config.get('_ax_harness2_probe'):
                self._wait_dependencies(job)
                with job.run.lane_condition:
                    job.waiting_for='capacity'
                    while not self._can_start(job):
                        job.run.lane_condition.wait(min(.1,self._remaining(job)))
                    job.run.admitted[job.session_id] = job
                    job.waiting_for=None
            self._remaining(job)
            self._save(job, "TASK_STATE_WORKING")
            runner = self.runners[job.config['agent']]
            policy = None if job.config.get('_ax_harness2_probe') else role_policy(job.config)
            workflow = WorkflowRun(policy, job.task) if policy else None
            if workflow:
                workflow.deadline = min(workflow.deadline, job.deadline)
                job.deadline = workflow.deadline
                job.config['_workflow_coordinator'] = True
                job.config['timeout_seconds'] = policy['turn_seconds']
            if job.chat is None:
                job.chat = runner.start_chat(job.session_id, job.config,
                                             deadline=job.deadline, cancel_event=job.canceled)
            stage = "worker_message"
            # Adapters without checkpoint support retain their one-turn contract.
            if not hasattr(job.chat, 'get_checkpoint'):
                workflow = None
            message = job.task
            while True:
                if workflow:
                    if workflow.remaining() < RUNTIME['timeouts']['min_turn_seconds']:
                        result = workflow.stop({'answer': 'No budget remains for another worker turn',
                                                'outcome': workflow.meta.get('outcome')},
                                               'workflow_deadline_exhausted')
                        break
                    workflow.begin()
                    job.config['_ax_delegation']['workflow'] = {
                        'state': 'running', 'turns': workflow.meta['turns']}
                    self._save(job, 'TASK_STATE_WORKING')
                emit_progress('turn_started')
                result = job.chat.send(message, timeout_seconds=min(
                    self._remaining(job), policy['turn_seconds']) if workflow else self._remaining(job))
                if workflow:
                    terminal = workflow.record(result, job.chat.get_checkpoint())
                    job.config['_ax_delegation']['outcome'] = workflow.meta['outcome']
                    job.config['_ax_delegation']['outcomeRecordedAt'] = workflow.meta['outcomeRecordedAt']
                    job.config['_ax_delegation']['workflow'] = {
                        key: workflow.meta[key] for key in ('state', 'turns')}
                    if terminal is None:
                        servers = job.config.get('mcpServers', {})
                        setup_budget = self.setup_estimate(job.config) + RUNTIME['model']['final_answer_reserve_seconds']
                        if not job.run.persistent and servers and workflow.remaining() <= setup_budget:
                            result = {**result, 'outcome': {**result['outcome'],
                                'reason': 'parent_budget_checkpoint',
                                'remaining_work': [*result['outcome']['remaining_work'],
                                    'Resume this session on a fresh parent turn; budget cannot cover MCP rediscovery']}}
                            job.config['_ax_delegation']['workflow']['state'] = 'checkpoint'
                            break
                        job.messages += [{'role': 'user', 'content': message},
                                         {'role': 'assistant', 'content': result['answer']}]
                        self._save(job, 'TASK_STATE_WORKING')
                        if job.run.persistent:
                            self.await_children(job.session_id, job.chat, job.messages, job.deadline)
                            workflow.meta['checkpoint'] = job.chat.get_checkpoint()
                        message = workflow.continuation(compact=bool(getattr(job.chat,'supports_run_context',False)))
                        emit_progress('workflow_checkpoint', turn=workflow.meta['turns'])
                        continue
                    result = terminal
                break
            answer = result['answer']
            outcome = result.get('outcome', {'status': 'completed', 'reason': 'answer_returned'})
            if not isinstance(outcome, dict) or outcome.get('status') not in {'completed', 'partial', 'blocked', 'failed'}:
                raise AgentRunError('worker returned an invalid task outcome')
            job.config['_ax_delegation']['outcome'] = outcome
            job.config['_ax_delegation']['outcomeRecordedAt'] = datetime.now(timezone.utc).isoformat()
            self._remaining(job)
            state = "TASK_STATE_COMPLETED"
        except DependencyBlocked as exc:
            state, answer = 'TASK_STATE_COMPLETED',str(exc)
            job.config['_ax_delegation']['outcome'] = {'status':'blocked','reason':'dependency_not_completed',
                'evidence':[],'errors':[str(exc)],'remaining_work':['Resolve the dependency before submitting replacement work']}
            job.config['_ax_delegation']['outcomeRecordedAt'] = datetime.now(timezone.utc).isoformat()
        except TimeoutError:
            state, issue = "TASK_STATE_CANCELED", "delegation canceled or deadline exceeded"
        except Exception as exc:
            issue, failure = _worker_failure(exc, stage)
        finally:
            try:
                resumable = (job.chat is not None and not self.closed and state == 'TASK_STATE_COMPLETED' and
                    not job.config.get('_ax_harness2_probe') and
                    job.config['_ax_delegation'].get('outcome', {}).get('status') in {'partial', 'blocked', 'failed'}
                    and not job.canceled.is_set() and not job.run.canceled.is_set())
                if job.chat:
                    if resumable:
                        job.chat.after_turn()
                    else:
                        job.chat.close()
                if job.canceled.is_set() or job.run.canceled.is_set():
                    state, answer, issue = "TASK_STATE_CANCELED", None, "delegation canceled"
                    resumable = False
                    if job.chat:
                        job.chat.close()
                emit_progress('completed' if state == 'TASK_STATE_COMPLETED' else 'failed')
                job.config['_ax_delegation']['resumable'] = resumable
                if state != 'TASK_STATE_COMPLETED':
                    prior = (workflow.meta.get('outcome') or {}) if workflow else {}
                    outcome = {**prior, 'status': 'failed', 'reason': issue}
                    if prior:
                        outcome['errors'] = [*prior.get('errors', []), issue]
                        outcome['remaining_work'] = [*prior.get('remaining_work', []),
                            'Partial work may have persisted; reconcile it before repeating external actions']
                        last_answer = next((item['content'] for item in reversed(job.messages)
                                            if item['role'] == 'assistant'), '')
                        limit = RUNTIME['limits']['tool_output_chars'] // 2
                        outcome['last_partial_answer'] = last_answer[:limit]
                        outcome['last_partial_answer_truncated'] = len(last_answer) > limit
                    job.config['_ax_delegation']['outcome'] = outcome
                    job.config['_ax_delegation']['outcomeRecordedAt'] = datetime.now(timezone.utc).isoformat()
                self._cancel_children(job.session_id)
                with job.run.lane_condition:
                    if job.run.admitted.get(job.session_id) is job:
                        job.run.admitted.pop(job.session_id)
                    job.run.lane_condition.notify_all()
                with self.lock:
                    if resumable and not job.chat.closed and not job.canceled.is_set() and not job.run.canceled.is_set():
                        self.retained[job.session_id] = (job.chat, time.monotonic())
                    else:
                        self.revoke(job.session_id)
                    self._save(job, state, answer if state == "TASK_STATE_COMPLETED" else None,
                               issue, failure)
                    self.jobs.pop(job.session_id, None)
            finally:
                with job.run.lane_condition:
                    if job.run.admitted.get(job.session_id) is job:
                        job.run.admitted.pop(job.session_id)
                    job.run.lane_condition.notify_all()
                with self.lock:
                    if self.jobs.get(job.session_id) is job:
                        self.jobs.pop(job.session_id)

    def _can_start(self, job):
        if job.run.pool is None:
            return True
        active=list(job.run.admitted.values())
        proposed=[*active,job]
        names={getattr(item.chat,'container_name',f'popot-chat-{item.session_id}') for item in proposed}
        retained=job.run.pool.reserved_workers(job.run.reservation)-names
        slots=job.run.reserved_slots-len(retained)
        depth=len(job.config['_ax_delegation']['lineage'])
        frontier=max((len(item.config['_ax_delegation']['lineage']) for item in active),default=1)
        depth_limit=min(self.policy['max_depth']+1,frontier+max(0,slots-len(active)))
        if depth>depth_limit:
            raise AgentRunError('Reserved capacity cannot support this nesting depth; no worker started')
        deepest=max(len(item.config['_ax_delegation']['lineage']) for item in proposed)
        # Keep a complete path to a leaf available, even while ancestors wait.
        future=max(0,depth_limit-deepest) if any(item.config.get('allowed_roles')
            for item in proposed if len(item.config['_ax_delegation']['lineage']) == deepest) else 0
        return len(proposed)+future<=slots

    def _wait_dependencies(self, job):
        ids=job.config['_ax_delegation'].get('depends_on',[])
        if not ids:return
        job.waiting_for='dependencies'
        while True:
            self._remaining(job)
            reports=[]
            with self.lock:
                for sid in ids:
                    saved=self.store.get(sid)
                    meta=(saved.get('roleConfig') or {}).get('_ax_delegation',{}) if saved else {}
                    if meta.get('state') in ACTIVE:
                        if sid not in self.jobs:
                            raise DependencyBlocked('Dependency execution is uncertain after restart: '+sid)
                        continue
                    if meta.get('outcome',{}).get('status')!='completed':
                        raise DependencyBlocked('Dependency did not complete successfully: '+sid)
                    outcome=meta.get('outcome',{})
                    reports.append({'sessionId':sid,'role':saved['role'],
                        'answer':meta.get('answer','')[:2000],
                        'outcome':{**{k:v for k,v in outcome.items() if k in {'status','reason','revision'}},
                            'evidence':[text[:400] for text in outcome.get('evidence',[])[:4] if isinstance(text,str)],
                            'checks':outcome.get('checks',[])[:4]}})
            if len(reports)==len(ids):
                job.config['_ax_delegation']['dependency_results']=reports
                job.waiting_for=None
                return
            with job.run.lane_condition:job.run.lane_condition.wait(min(.1,self._remaining(job)))

    def _task(self, saved: dict) -> dict:
        meta = saved["roleConfig"]["_ax_delegation"]
        task = {"id": saved["sessionId"], "contextId": meta["root_session_id"],
                "status": {"state": meta["state"], "timestamp": saved["updatedAt"]},
                "metadata": {"sessionId": saved["sessionId"], "role": saved["role"],
                             "parentSessionId": meta["parent_session_id"]}}
        task['metadata']['durable'] = meta.get('durable', False)
        if meta.get('outcomeRecordedAt'):
            task['metadata']['reportedAt'] = meta['outcomeRecordedAt']
        if meta.get('outcome'):
            task['metadata']['outcome'] = meta['outcome']
        activity = meta.get('activity', [])
        with self.lock:
            job = self.jobs.get(saved['sessionId'])
            retained = self.retained.get(saved['sessionId'])
            task['metadata']['resumable'] = bool(meta.get('resumable') and retained and not retained[0].closed)
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
            retained = list(self.retained.values())
            self.retained.clear()
        for chat, _ in retained:
            chat.close()
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

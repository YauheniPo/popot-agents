"""Execute coordination turns with the root and child persistence contracts."""

import subprocess
import time
from contextlib import ExitStack

from popot_agents.progress import emit_progress, trace_event
from popot_agents.runtime_config import RUNTIME
from .errors import AgentRunError
from .workflow import include_interventions


def _at_implementation_boundary(remaining):
    return remaining is not None and remaining <= (
        RUNTIME['timeouts']['turn_response_margin_seconds'] + RUNTIME['timeouts']['min_turn_seconds'])


def _run_turns(execution):
    while True:
        execution.stage = 'begin_turn'
        terminal = execution.begin()
        if terminal is not None:
            return terminal
        emit_progress('turn_started')
        execution.stage = 'worker_send'
        result = execution.send()
        terminal = execution.record_result(result)
        if terminal is not None:
            return terminal
        terminal = execution.advance(result)
        if terminal is not None:
            return terminal


class _RootExecution:
    def __init__(self, server, session_id, record, message, workflow, role_config, delegation):
        self.server, self.session_id, self.record = server, session_id, record
        self.message, self.workflow, self.delegation = message, workflow, delegation
        self.config = record.role_config if role_config is None else role_config
        self.stage = 'delegation_setup'

    def save(self, history):
        self.server.store.save(self.session_id, self.record.agent, history, self.record.role, self.config)

    def discard_worker(self):
        self.record.worker.close()
        with self.server.live_lock:
            self.server.live.pop(self.session_id, None)

    def begin(self):
        workflow = self.workflow
        if workflow is None:
            return None
        if workflow.remaining() < RUNTIME['timeouts']['min_turn_seconds'] or (
                workflow.meta['turns'] >= workflow.policy['max_turns'] and not workflow.meta.get('awaiting_children')):
            result = workflow.stop({'answer': 'Autonomous workflow time budget exhausted',
                                    'outcome': workflow.meta.get('outcome')},
                'workflow_deadline_exhausted' if workflow.remaining() < RUNTIME['timeouts']['min_turn_seconds']
                else 'workflow_turn_limit')
            self.config = {**(self.config or {}), '_workflow': workflow.meta}
            self.save(self.record.messages)
            return result
        workflow.begin()
        self.config = {**(self.config or {}), '_workflow': workflow.meta}
        self.save(self.record.messages)

    def implementation_remaining(self):
        if (self.workflow and not self.workflow.meta.get('verificationPhaseStarted')
                and hasattr(self.delegation, 'implementation_remaining')):
            return self.delegation.implementation_remaining(self.session_id)
        return None

    def send(self):
        if not self.workflow:
            return self.record.worker.send(self.message.strip())
        timeout = min(self.workflow.policy['turn_seconds'], self.workflow.remaining())
        implementation = self.implementation_remaining()
        if implementation is not None and implementation > 0:
            timeout = min(timeout, implementation)
        return self.record.worker.send(self.message.strip(), timeout_seconds=timeout)

    def record_result(self, result):
        workflow, record = self.workflow, self.record
        intervention_ids = result.get('interventionIds', [])
        terminal = result
        if workflow:
            workflow.meta['authorization'] = include_interventions(workflow.meta['authorization'],
                self.server.store.recent_interventions(self.session_id), intervention_ids)
            self.stage = 'record_result'
            checkpoint = record.worker.get_checkpoint() if hasattr(record.worker, 'get_checkpoint') else None
            yield_for_verification = _at_implementation_boundary(self.implementation_remaining())
            terminal = workflow.record(result, checkpoint, yield_for_verification=yield_for_verification)
            if terminal is not None:
                result = terminal
            if (yield_for_verification and terminal is not None
                    and terminal.get('outcome', {}).get('status') == 'partial'):
                workflow.meta.update(state='running', awaiting_children=True, verificationPhaseStarted=True)
                terminal = None
            workflow.meta['waiting_for'] = 'children' if terminal is None and result.get('outcome', {}).get('reason') == 'child_pending' else None
        history = record.messages + [{'role': 'user', 'content': self.message.strip()},
                                     {'role': 'assistant', 'content': result['answer']}]
        emit_progress('saving')
        self.stage = 'save_result'
        self.save(history)
        self.server.store.acknowledge_interventions(self.session_id, intervention_ids)
        record.messages, record.role_config = history, self.config
        record.last_used = time.monotonic()
        return result if terminal is not None else None

    def advance(self, result):
        workflow, record = self.workflow, self.record
        emit_progress('workflow_checkpoint', turn=workflow.meta['turns'])
        if self.delegation:
            self.stage = 'waiting_for_children'
            self.delegation.await_children(self.session_id, record.worker, record.messages, workflow.deadline)
            workflow.meta['waiting_for'] = None
            workflow.meta['checkpoint'] = record.worker.get_checkpoint()
            self.save(record.messages)
        try:
            self.stage = 'prepare_continuation'
            self.message = workflow.continuation(compact=bool(getattr(record.worker, 'supports_run_context', False)))
        except ValueError:
            result = workflow.stop(result, 'workflow_context_limit')
            self.save(record.messages)
            return result

    def interrupted(self, exc):
        workflow = self.workflow
        checkpoint = workflow.meta.get('checkpoint')
        checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
        pending = [value.get('session_id') for _, value in checkpoint.get('failed_delegations', [])
                   if isinstance(value, dict) and value.get('pending')]
        if isinstance(exc, AgentRunError):
            code = 'agent_run_error'
        elif isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
            code = 'child_wait_interrupted' if self.stage == 'waiting_for_children' else 'worker_timeout'
        else:
            code = 'os_error'
        failure = {'code': code, 'stage': self.stage, 'error_type': type(exc).__name__,
                   'childSessionIds': list(dict.fromkeys(sid for sid in pending if isinstance(sid, str))),
                   'deadlineExpired': workflow.remaining() <= 0}
        diagnostic = getattr(exc, 'diagnostic', None)
        if isinstance(diagnostic, dict):
            failure['diagnostic'] = {key: diagnostic[key] for key in
                ('task', 'phase', 'reason', 'operation', 'command', 'exit_code', 'probe') if key in diagnostic}
        if isinstance(exc, OSError) and type(exc.errno) is int:
            failure['errno'] = exc.errno
        trace_event('workflow_interrupted', session_id=self.session_id, **failure)
        prior = workflow.meta.get('outcome')
        prior = prior if isinstance(prior, dict) else {}
        errors = prior.get('errors') if isinstance(prior.get('errors'), list) else []
        remaining = prior.get('remaining_work') if isinstance(prior.get('remaining_work'), list) else []
        return workflow.stop({'answer': 'Worker execution interrupted; reconcile effects before retrying',
            'outcome': {**prior, 'failure': failure,
                        'errors': [*errors, f'{failure["error_type"]} at {self.stage}'],
                        'remaining_work': ['Reconcile interrupted execution and external effects before continuing',
                                           *remaining]}}, 'workflow_execution_uncertain')


def run_root(server, session_id, record, message, workflow=None, *, role_config=None, delegation=None):
    """Run an admitted, locked root session; persist before continuing or suspending.

    The caller owns admission, worker startup/restore and the HTTP reply. An
    uncertain execution is saved as blocked and is never automatically replayed.
    """
    execution = _RootExecution(server, session_id, record, message, workflow, role_config, delegation)
    scope = ExitStack()
    try:
        if workflow and delegation:
            scope.enter_context(delegation.workflow(session_id, workflow.deadline,
                max_turns=workflow.policy['max_turns'], metadata=workflow.meta))
            execution.stage = 'waiting_for_children'
            delegation.await_children(session_id, record.worker, record.messages, workflow.deadline)
        result = _run_turns(execution)
        execution.stage = 'after_turn'
        if hasattr(record.worker, 'after_turn'):
            record.worker.after_turn()
    except AgentRunError as exc:
        if workflow:
            result = execution.interrupted(exc)
            execution.save(record.messages)
        if not record.worker.is_alive():
            execution.discard_worker()
        if not workflow:
            raise
    except (subprocess.TimeoutExpired, OSError) as exc:
        if workflow:
            result = execution.interrupted(exc)
            execution.save(record.messages)
        execution.discard_worker()
        if not workflow:
            raise
    finally:
        # Root cleanup must finish before closing the delegation scope.
        scope.close()
    return result


class _ChildExecution:
    def __init__(self, service, job, workflow, intervention_ids):
        self.service, self.job, self.workflow = service, job, workflow
        self.intervention_ids = intervention_ids
        self.message = job.task

    def begin(self):
        workflow, job = self.workflow, self.job
        if workflow is None:
            return None
        self.service._remaining(job)
        if workflow.remaining() < RUNTIME['timeouts']['min_turn_seconds']:
            return workflow.stop({'answer': 'No budget remains for another worker turn',
                                  'outcome': workflow.meta.get('outcome')}, 'workflow_deadline_exhausted')
        previous = job.config['_ax_delegation'].get('workflow', {})
        if isinstance(previous.get('checkpoint'), dict):
            workflow.meta.setdefault('checkpoint', previous['checkpoint'])
        while _at_implementation_boundary(self.service.implementation_remaining(job.session_id)):
            outcome = workflow.meta.get('outcome') or job.chat.get_checkpoint().get('outcome') or {}
            result = {'answer': 'Initial implementation window ended; checkpoint retained for verification.',
                'outcome': {**outcome, 'status': 'partial', 'evidence': outcome.get('evidence', []),
                    'errors': outcome.get('errors', []), 'remaining_work': outcome.get('remaining_work') or
                    ['Arrange independent review and QA before resuming corrective work']}}
            terminal = self.record_result(result)
            if terminal is not None:
                return terminal
            self.advance(result)
            self.service._remaining(job)
        workflow.begin()
        job.config['_ax_delegation']['workflow'] = {key: workflow.meta[key]
            for key in ('state', 'turns', 'checkpoint') if key in workflow.meta}
        self.service._save(job, 'TASK_STATE_WORKING')

    def send(self):
        timeout = self.service._remaining(self.job)
        if self.workflow:
            timeout = min(timeout, self.workflow.policy['turn_seconds'])
            implementation = self.service.implementation_remaining(self.job.session_id)
            if implementation is not None:
                timeout = min(timeout, implementation)
        return self.job.chat.send(self.message, timeout_seconds=timeout)

    def record_result(self, result):
        workflow, job = self.workflow, self.job
        self.intervention_ids[:] = list(dict.fromkeys([*self.intervention_ids, *result.get('interventionIds', [])]))
        meta = job.config['_ax_delegation']
        meta['scope_assignments'] = include_interventions(meta.get('scope_assignments', []),
            self.service.store.recent_interventions(job.session_id), self.intervention_ids)
        if workflow is None:
            return result
        terminal = workflow.record(result, job.chat.get_checkpoint(), yield_for_verification=
            _at_implementation_boundary(self.service.implementation_remaining(job.session_id)))
        meta['outcome'] = workflow.meta['outcome']
        meta['outcomeRecordedAt'] = workflow.meta['outcomeRecordedAt']
        meta['workflow'] = {key: workflow.meta[key]
            for key in ('state', 'turns', 'checkpoint') if key in workflow.meta}
        if terminal is not None:
            return terminal
        servers = job.config.get('mcpServers', {})
        setup_budget = self.service.setup_estimate(job.config) + RUNTIME['model']['final_answer_reserve_seconds']
        if not job.run.persistent and servers and workflow.remaining() <= setup_budget:
            meta['workflow']['state'] = 'checkpoint'
            return {**result, 'outcome': {**result['outcome'], 'reason': 'parent_budget_checkpoint',
                'remaining_work': [*result['outcome']['remaining_work'],
                    'Resume this session on a fresh parent turn; budget cannot cover MCP rediscovery']}}
        job.messages += [{'role': 'user', 'content': self.message},
                         {'role': 'assistant', 'content': result['answer']}]
        self.service._save(job, 'TASK_STATE_WORKING')

    def advance(self, result):
        workflow, job = self.workflow, self.job
        if job.run.persistent:
            self.service.await_children(job.session_id, job.chat, job.messages, job.deadline)
            workflow.meta['checkpoint'] = job.chat.get_checkpoint()
        self.message = workflow.continuation(compact=bool(getattr(job.chat, 'supports_run_context', False)))
        emit_progress('workflow_checkpoint', turn=workflow.meta['turns'])


def run_child(service, job, workflow, intervention_ids):
    """Run an admitted child; the caller retains terminal persistence and cleanup.

    Keep the workflow created before startup and mutate consumed IDs in place,
    so the caller can preserve partial evidence and acknowledge IDs on failure.
    """
    return _run_turns(_ChildExecution(service, job, workflow, intervention_ids))

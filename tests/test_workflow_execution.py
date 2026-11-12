"""Coordination must preserve receipts before advancing or parking a worker."""
from contextlib import nullcontext
from copy import deepcopy
import threading
from types import SimpleNamespace

import pytest

from ax_local.api.delegation import DelegationService, Job, Run
from popot_agents.orchestrator.main import AgentRunError, ChatRecord
from popot_agents.orchestrator.session_store import SessionStore
from popot_agents.orchestrator.workflow import WorkflowRun
from popot_agents.orchestrator.workflow_execution import run_root, run_child


SESSION = 'a' * 16
CHILD = 'b' * 16
POLICY = {'max_turns': 4, 'total_seconds': 300, 'turn_seconds': 60,
          'max_no_progress': 2, 'max_concurrent': 1}
CHECKPOINT = {'messages': [{'role': 'tool', 'content': 'publication receipt'}]}


def report(status, *, reason='fixture'):
    return {'answer': status, 'outcome': {'status': status, 'reason': reason,
            'evidence': ['publication receipt'], 'errors': [],
            'remaining_work': [] if status == 'completed' else ['verify publication']}}


class FaultStore(SessionStore):
    fail_when = None

    def save(self, session_id, agent, messages, role, config):
        if self.fail_when and self.fail_when(messages, config):
            self.fail_when = None
            raise OSError('fixture storage unavailable')
        return super().save(session_id, agent, messages, role, config)


class Worker:
    container_name = 'fixture-worker'

    def __init__(self, store, steps):
        self.store, self.steps = store, list(steps)
        self.calls, self.checkpoint = [], {}
        self.suspends = self.closes = 0
        self.before_send = lambda: None

    def send(self, message, **_options):
        self.before_send()
        self.calls.append(message)
        result, checkpoint = self.steps.pop(0)
        if isinstance(result, Exception):
            raise result
        self.checkpoint = deepcopy(checkpoint)
        self.last_answer = result['answer']
        return deepcopy(result)

    def get_checkpoint(self):
        return deepcopy(self.checkpoint)

    def after_turn(self):
        assert self.store.get(SESSION)['messages'][-1]['content'] == self.last_answer
        self.suspends += 1

    def is_alive(self):
        return self.closes == 0

    def close(self):
        self.closes += 1


def execution(mode, directory, steps, *, max_turns=4):
    store = FaultStore(directory)
    workflow = WorkflowRun({**POLICY, 'max_turns': max_turns}, 'publish once')
    worker = Worker(store, steps)
    record = ChatRecord('fixture', worker, [], 'backend_engineer',
                        {'agent': 'fixture', '_workflow': workflow.meta})
    job = Job(SESSION, {'agent': 'fixture', '_ax_role': 'backend_engineer',
              '_ax_delegation': {'parent_session_id': 'c' * 16, 'scope_assignments': ['publish once']}},
              'publish once', Run(workflow.deadline, persistent=True), workflow.deadline, chat=worker)
    server = SimpleNamespace(store=store, live={SESSION: record}, live_lock=threading.RLock())
    service = DelegationService({}, {}, {}, store, 'http://fixture.invalid')
    service.await_children = lambda *_: False
    context = SimpleNamespace(store=store, workflow=workflow, worker=worker, record=record,
                              job=job, service=service, delegation=None, intervention_ids=[])
    config = record.role_config if mode == 'root' else job.config
    store.save(SESSION, 'fixture', [], 'backend_engineer', config)

    def invoke():
        if mode == 'root':
            return run_root(server, SESSION, record, 'publish once', workflow,
                            role_config=record.role_config, delegation=context.delegation)
        return run_child(service, job, workflow, context.intervention_ids)

    context.invoke = invoke
    return context


@pytest.mark.parametrize('mode', ['root', 'child'])
def test_partial_is_saved_before_next_send_and_root_parks_only_after_final_save(mode, tmp_path):
    context = execution(mode, tmp_path, [(report('partial'), CHECKPOINT), (report('completed'), {})])

    def verify_before_send():
        saved = context.store.get(SESSION)
        if context.worker.calls:
            assert saved['messages'][-1]['content'] == 'partial'
            meta = saved['roleConfig']['_workflow' if mode == 'root' else '_ax_delegation']
            assert meta['outcome']['evidence'] == ['publication receipt']
        else:
            config = saved['roleConfig']
            meta = config['_workflow'] if mode == 'root' else config['_ax_delegation']['workflow']
            assert meta['turns'] == 1

    context.worker.before_send = verify_before_send
    result = context.invoke()

    assert result['outcome']['status'] == 'completed'
    assert len(context.worker.calls) == 2
    assert 'Do not repeat' in context.worker.calls[1]
    assert context.workflow.meta['turns'] == 2
    assert context.worker.suspends == (1 if mode == 'root' else 0)
    if mode == 'child':
        assert len(context.job.messages) == 2  # Final child publication belongs to its finalizer.


@pytest.mark.parametrize('mode', ['root', 'child'])
def test_failure_saving_executing_prevents_worker_send(mode, tmp_path):
    context = execution(mode, tmp_path, [(report('completed'), {})])
    context.store.fail_when = lambda messages, _config: not messages

    if mode == 'child':
        with pytest.raises(OSError, match='fixture storage unavailable'):
            context.invoke()
    else:
        result = context.invoke()
        assert result['outcome']['reason'] == 'workflow_execution_uncertain'

    assert context.worker.calls == []
    assert context.worker.suspends == 0


@pytest.mark.parametrize('mode', ['root', 'child'])
def test_failure_saving_partial_never_starts_next_turn_or_parks_worker(mode, tmp_path):
    context = execution(mode, tmp_path, [(report('partial'), CHECKPOINT), (report('completed'), {})])
    context.store.fail_when = lambda messages, _config: len(messages) == 2

    if mode == 'child':
        with pytest.raises(OSError, match='fixture storage unavailable'):
            context.invoke()
    else:
        result = context.invoke()
        assert result['outcome']['status'] == 'blocked'
        assert result['outcome']['evidence'] == ['publication receipt']

    assert context.worker.calls == ['publish once']
    assert context.worker.suspends == 0


@pytest.mark.parametrize('mode', ['root', 'child'])
def test_uncertain_send_keeps_prior_evidence_and_consumed_comments_without_replay(mode, tmp_path):
    partial = report('partial')
    context = execution(mode, tmp_path, [(partial, CHECKPOINT),
                        (AgentRunError('response lost after publication'), None)])
    included = context.store.enqueue_intervention(SESSION, 'Verify the published revision')
    context.store.enqueue_intervention(SESSION, 'Still queued')
    partial['interventionIds'] = [included['id']]

    if mode == 'child':
        with pytest.raises(AgentRunError, match='response lost after publication'):
            context.invoke()
        meta = context.job.config['_ax_delegation']
        assert context.intervention_ids == [included['id']]
        authority = meta['scope_assignments']
    else:
        result = context.invoke()
        assert result['outcome']['reason'] == 'workflow_execution_uncertain'
        meta = context.store.get(SESSION)['roleConfig']['_workflow']
        authority = meta['authorization']

    assert len(context.worker.calls) == 2
    assert context.worker.calls[1] != 'publish once'
    assert meta['outcome']['evidence'] == ['publication receipt']
    assert 'verify publication' in meta['outcome']['remaining_work']
    assert 'Verify the published revision' in authority
    assert 'Still queued' not in authority
    assert context.worker.suspends == 0


def pending_checkpoint():
    return {**CHECKPOINT, 'failed_delegations': [[['qa_engineer', 'verify'],
            {'session_id': CHILD, 'pending': True}]]}


def install_wait(context, wait):
    context.service.await_children = wait
    context.delegation = SimpleNamespace(workflow=lambda *_args, **_kwargs: nullcontext(),
                                         await_children=wait)


@pytest.mark.parametrize('mode', ['root', 'child'])
def test_waiting_for_children_continues_the_same_last_coordination_turn(mode, tmp_path):
    context = execution(mode, tmp_path, [(report('partial', reason='child_pending'), pending_checkpoint()),
                        (report('completed'), {})], max_turns=1)
    deliveries = []

    def wait(_session, worker, history, _deadline):
        if not worker.checkpoint.get('failed_delegations'):
            return False
        assert context.store.get(SESSION)['messages'] == history
        assert history[-1]['content'] == 'partial'
        deliveries.append(CHILD)
        worker.checkpoint = deepcopy(CHECKPOINT)
        return True

    install_wait(context, wait)
    result = context.invoke()

    assert result['outcome']['status'] == 'completed'
    assert len(context.worker.calls) == 2
    assert deliveries == [CHILD]
    assert context.workflow.meta['turns'] == 1


@pytest.mark.parametrize('mode', ['root', 'child'])
def test_wait_failure_preserves_partial_receipts_and_does_not_send_again(mode, tmp_path):
    checkpoint = pending_checkpoint()
    context = execution(mode, tmp_path, [(report('partial', reason='child_pending'), checkpoint)])

    def wait(_session, worker, _history, _deadline):
        if worker.checkpoint.get('failed_delegations'):
            raise TimeoutError('fixture child wait interrupted')
        return False

    install_wait(context, wait)
    if mode == 'child':
        with pytest.raises(TimeoutError, match='fixture child wait interrupted'):
            context.invoke()
        outcome = context.job.config['_ax_delegation']['outcome']
    else:
        result = context.invoke()
        outcome = result['outcome']
        assert outcome['failure']['stage'] == 'waiting_for_children'
        assert outcome['failure']['childSessionIds'] == [CHILD]

    assert context.worker.calls == ['publish once']
    assert outcome['evidence'] == ['publication receipt']
    assert context.workflow.meta['checkpoint'] == checkpoint
    assert context.worker.suspends == 0

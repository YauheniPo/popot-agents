"""Verification time belongs to the workflow, not a child's immutable deadline."""
from contextlib import contextmanager
import time
from unittest.mock import patch

import pytest

from ax_local.api.delegation import A2AError, Job
from ax_local.api.worker_queue import WorkerQueue
from ax_local.tests import test_delegation as delegation_tests


@pytest.fixture
def helper():
    fixture = delegation_tests.DelegationTests()
    fixture.setUp()
    fixture.roles['tech_lead']['allowed_roles'].append('qa_engineer')
    fixture.roles['analyst'] = {**fixture.roles['qa_engineer'], '_ax_role': 'analyst'}
    fixture.roles['backend_engineer']['allowed_roles'].append('analyst')
    yield fixture
    fixture.doCleanups()


@contextmanager
def workflow(helper, *, root=None, turns=1):
    root = root or helper.parent
    metadata = {'turns': turns, 'run_id': 'verification-budget',
                'deadlineAt': time.time() + 3600,
                'implementationDeadlineAt': time.time() + 2700,
                'verificationReserveSeconds': 900}
    with helper.service.workflow(root, time.monotonic() + 3600, max_turns=4,
                                 metadata=metadata) as run, helper.service.turn(root, 3600):
        yield run


def save_verification(helper, run, *, role='qa_engineer', parent=None, run_id=None,
                      state='TASK_STATE_COMPLETED', status='completed', evidence=None):
    meta = {'parent_session_id': parent or helper.parent, 'root_session_id': helper.parent,
            'run_id': run_id or run.metadata['run_id'], 'state': state,
            'outcome': {'status': status, 'verification_status': 'failed',
                        'evidence': ['Finding F1'] if evidence is None else evidence}}
    helper.store.save('f' * 16, 'test', [], role, {'_ax_delegation': meta})


def test_implementation_keeps_hard_deadline_and_verification_gets_full_budget(helper):
    with workflow(helper) as run:
        implementation = helper.wait_task(helper.send()['id'])
        verification = helper.wait_task(helper.send(role='qa_engineer', message_id='qa')['id'], role='qa_engineer')
        implementation_meta = helper.store.get(implementation['id'])['roleConfig']['_ax_delegation']
        verification_meta = helper.store.get(verification['id'])['roleConfig']['_ax_delegation']
        assert implementation_meta['implementationDeadlineAt'] == run.metadata['implementationDeadlineAt']
        assert implementation_meta['deadlineAt'] > run.metadata['implementationDeadlineAt'] + 700
        assert 'implementationDeadlineAt' not in verification_meta
        assert verification_meta['deadlineAt'] > run.metadata['implementationDeadlineAt'] + 700


@pytest.mark.parametrize('changed,expected', [({}, True), ({'run_id': 'old-run'}, False),
    ({'parent': 'e' * 16}, False), ({'state': 'TASK_STATE_WORKING'}, False),
    ({'status': 'partial'}, False), ({'evidence': []}, False), ({'evidence': ['  ']}, False),
    ({'role': 'backend_engineer'}, False)])
def test_only_completed_same_run_owner_verification_unlocks_corrections(helper, changed, expected):
    with workflow(helper) as run:
        save_verification(helper, run, **changed)
        assert helper.service.verification_completed(run) is expected
        task = helper.wait_task(helper.send()['id'])
        meta = helper.store.get(task['id'])['roleConfig']['_ax_delegation']
        assert ('implementationDeadlineAt' not in meta) is expected


def test_correction_resumes_same_sandbox_without_extending_hard_deadline(helper):
    helper.runner.callback = lambda *_: {'answer': 'checkpoint', 'outcome': {'status': 'partial'}}
    with workflow(helper) as run:
        first = helper.wait_task(helper.send()['id'])
        sid = first['id']
        original_chat = helper.service.retained[sid][0]
        original_chat.files['patch'] = 'preserved'
        initial = helper.store.get(sid)['roleConfig']['_ax_delegation']
        assert 'implementationDeadlineAt' in initial
        for reviewed in (False, True):
            if reviewed:
                save_verification(helper, run)
            helper.service.dispatch(helper.token, 'backend_engineer', 'SendMessage', {'message': {
                'messageId': 'resume-' + str(reviewed), 'taskId': sid, 'role': 'ROLE_USER',
                'parts': [{'text': 'continue required work'}]}})
            helper.wait_task(sid)
            current = helper.store.get(sid)['roleConfig']['_ax_delegation']
            assert ('implementationDeadlineAt' not in current) is reviewed
            assert current['deadlineAt'] == pytest.approx(initial['deadlineAt'], abs=.05)
            assert helper.service.retained[sid][0] is original_chat
            assert original_chat.files['patch'] == 'preserved'


def test_nested_work_inherits_implementation_cutoff_even_after_a_review(helper):
    observed = []
    def callback(chat, _task):
        if chat.config['_ax_role'] == 'backend_engineer':
            save_verification(helper, chat.runner.delegation.grants[chat.session_id].run)
            token = helper.service.worker_environment(chat.session_id, chat.config)['AX_DELEGATION_TOKEN']
            nested = helper.wait_task(helper.send(token=token, role='analyst', message_id='nested')['id'],
                                      token=token, role='analyst')
            observed.append(helper.store.get(nested['id'])['roleConfig']['_ax_delegation'])
        return {'answer': 'done', 'outcome': {'status': 'completed'}}
    helper.runner.callback = callback
    with workflow(helper) as run:
        helper.wait_task(helper.send()['id'])
        assert observed[0]['implementationDeadlineAt'] == run.metadata['implementationDeadlineAt']


def test_budget_context_exposes_reserve_without_limiting_planner(helper):
    with workflow(helper):
        context = helper.service.turn_context(helper.parent)['budget']
        assert context['workflow_seconds_remaining'] == pytest.approx(3600, abs=1)
        assert context['implementation_seconds_remaining'] == pytest.approx(2700, abs=1)
        assert context['verification_reserve_seconds'] == 900
        assert context['implementation_limited'] is False
        assert context['verification_started'] is False


def test_standalone_implementer_limited_until_own_verification_is_complete(helper):
    root = 'b' * 16
    helper.service.worker_environment(root, helper.roles['backend_engineer'])
    with workflow(helper, root=root) as run:
        assert helper.service.implementation_remaining(root) == pytest.approx(2700, abs=1)
        assert helper.service.turn_context(root)['budget']['implementation_limited'] is True
        save_verification(helper, run, parent=root)
        assert helper.service.implementation_remaining(root) is None
        assert helper.service.turn_context(root)['budget']['verification_started'] is True


def test_final_coordinator_turn_can_delegate_verification(helper):
    with workflow(helper, turns=4):
        assert helper.service.turn_context(helper.parent)['can_delegate'] is True
        task = helper.wait_task(helper.send(role='qa_engineer')['id'], role='qa_engineer')
        assert task['metadata']['outcome']['status'] == 'completed'


def test_expired_initial_budget_rejects_before_creating_worker_or_consuming_call(helper):
    with workflow(helper) as run:
        run.metadata['implementationDeadlineAt'] = run.metadata['deadlineAt'] - 3601
        with pytest.raises(A2AError, match='review|QA'):
            helper.send()
        assert helper.runner.chats == []
        assert run.calls == 0
        save_verification(helper, run)
        task = helper.wait_task(helper.send(message_id='correct')['id'])
        assert task['metadata']['outcome']['status'] == 'completed'


def test_initial_budget_must_cover_measured_setup_and_final_answer(helper):
    helper.roles['backend_engineer']['mcpServers'] = {'repository': {}}
    with workflow(helper) as run:
        run.metadata['implementationDeadlineAt'] = run.metadata['deadlineAt'] - 3590
        with pytest.raises(A2AError, match='budget'):
            helper.send()
        assert helper.runner.chats == []
        assert run.calls == 0


def test_initial_budget_must_cover_response_margin_before_worker_creation(helper):
    with workflow(helper) as run:
        run.metadata['implementationDeadlineAt'] = run.metadata['deadlineAt'] - 3590
        with pytest.raises(A2AError, match='budget'):
            helper.send()
        assert helper.runner.chats == []
        assert run.calls == 0


def test_disabled_delegation_does_not_reserve_time_for_inaccessible_verification(helper):
    root = 'b' * 16
    config = {**helper.roles['backend_engineer'], '_disable_delegation': True}
    helper.service.worker_environment(root, config)
    with workflow(helper, root=root):
        assert helper.service.implementation_remaining(root) is None


def test_standalone_implementation_children_inherit_cutoff_except_verifiers(helper):
    root = 'b' * 16
    token = helper.service.worker_environment(root, helper.roles['backend_engineer'])['AX_DELEGATION_TOKEN']
    with workflow(helper, root=root) as run:
        for role in ('analyst', 'qa_engineer'):
            task = helper.wait_task(helper.send(token=token, role=role, message_id=role)['id'], token=token, role=role)
            meta = helper.store.get(task['id'])['roleConfig']['_ax_delegation']
            if role == 'analyst':
                assert meta['implementationDeadlineAt'] == run.metadata['implementationDeadlineAt']
            else:
                assert 'implementationDeadlineAt' not in meta


def test_soft_cutoff_returns_resumable_checkpoint_through_real_child_execution(helper):
    checkpoint = {'messages': [{'role': 'tool', 'content': 'patch saved'}]}
    with workflow(helper) as run:
        started = run.deadline - 3600
        clock = [started + 2655]
        calls = []
        original_send = delegation_tests.FakeChat.send
        def send(chat, task, *, timeout_seconds=None):
            calls.append(timeout_seconds)
            return original_send(chat, task, timeout_seconds=timeout_seconds)
        def callback(chat, _task):
            chat.files['patch'] = 'preserved'
            clock[0] = started + 2701
            return {'answer': 'patch saved; verification remains', 'outcome': {
                'status': 'partial', 'evidence': ['patch saved'], 'errors': [],
                'remaining_work': ['independent review and QA']}}
        helper.runner.callback = callback
        config = {**helper.roles['backend_engineer'], '_ax_delegation': {
            'parent_session_id': helper.parent, 'root_session_id': helper.parent,
            'lineage': ['tech_lead', 'backend_engineer'], 'run_id': run.metadata['run_id'],
            'implementationDeadlineAt': run.metadata['implementationDeadlineAt']}}
        job = Job('d' * 16, config, 'prepare change', run, started + 3480)
        helper.service.jobs[job.session_id] = job
        with patch.object(delegation_tests.FakeChat, 'send', send), \
             patch.object(delegation_tests.FakeChat, 'get_checkpoint', return_value=checkpoint, create=True), \
             patch('time.monotonic', side_effect=lambda: clock[0]):
            helper.service._execute(job)
        saved = helper.store.get(job.session_id)['roleConfig']['_ax_delegation']
        assert calls == [pytest.approx(45, abs=.1)]
        assert saved['outcome']['status'] == 'partial'
        assert saved['outcome']['reason'] == 'verification_reserve'
        assert saved['resumable'] is True
        assert saved['workflow']['checkpoint'] == checkpoint
        assert helper.service.retained[job.session_id][0].files['patch'] == 'preserved'


@pytest.mark.parametrize('waiting_for', ['capacity', 'dependencies'])
def test_waiting_implementation_yields_at_soft_deadline_without_consuming_reserve(helper, waiting_for):
    with workflow(helper) as run:
        started = time.monotonic()
        run.metadata['implementationDeadlineAt'] = run.metadata['deadlineAt'] - 3599.97
        config = {**helper.roles['backend_engineer'], '_ax_delegation': {
            'parent_session_id': helper.parent, 'root_session_id': helper.parent,
            'lineage': ['tech_lead', 'backend_engineer'],
            'implementationDeadlineAt': run.metadata['implementationDeadlineAt']}}
        job = Job('d' * 16, config, 'prepare change', run, started + .3)
        helper.service.jobs[job.session_id] = job
        if waiting_for == 'capacity':
            run.pool = pool = WorkerQueue(3, 1)
            run.reservation = 'budget'; run.reserved_slots = 2
            pool.acquire('root'); pool.reserve('budget', 2)
            for name in ('retained1', 'retained2'):
                pool.acquire(name, reservation='budget')
        else:
            dependency = Job('e' * 16, {**config, '_ax_delegation': {
                'parent_session_id': helper.parent, 'root_session_id': helper.parent,
                'state': 'TASK_STATE_WORKING'}}, 'dependency', run, started + .3)
            helper.service.jobs[dependency.session_id] = dependency
            helper.store.save(dependency.session_id, 'test', [], 'backend_engineer', dependency.config)
            config['_ax_delegation']['depends_on'] = [dependency.session_id]
        helper.service._execute(job)
        elapsed = time.monotonic() - started
        saved = helper.store.get(job.session_id)['roleConfig']['_ax_delegation']
        assert elapsed < .15
        assert helper.runner.chats == []
        assert saved['outcome']['status'] == 'partial'
        assert saved['outcome']['reason'] == 'verification_reserve'
        assert saved['outcome']['evidence'] == []


def test_worker_startup_uses_soft_deadline_and_classifies_its_expiry(helper):
    with workflow(helper) as run:
        started = time.monotonic()
        run.metadata['implementationDeadlineAt'] = run.metadata['deadlineAt'] - 3599.97
        config = {**helper.roles['backend_engineer'], '_ax_delegation': {
            'parent_session_id': helper.parent, 'root_session_id': helper.parent,
            'lineage': ['tech_lead', 'backend_engineer'],
            'implementationDeadlineAt': run.metadata['implementationDeadlineAt']}}
        job = Job('d' * 16, config, 'prepare change', run, started + .3)
        helper.service.jobs[job.session_id] = job
        clock, deadlines = [started], []
        def start(*_args, deadline, **_kwargs):
            deadlines.append(deadline)
            clock[0] = started + .04
            raise TimeoutError('startup deadline exhausted')
        helper.runner.start_chat = start
        with patch('time.monotonic', side_effect=lambda: clock[0]):
            helper.service._execute(job)
        saved = helper.store.get(job.session_id)['roleConfig']['_ax_delegation']
        assert deadlines == [pytest.approx(started + .03, abs=.01)]
        assert job.deadline == started + .3
        assert saved['outcome']['status'] == 'partial'
        assert saved['outcome']['reason'] == 'verification_reserve'
        assert saved['resumable'] is False


def test_cancellation_takes_precedence_over_soft_deadline_expiry(helper):
    with workflow(helper) as run:
        run.metadata['implementationDeadlineAt'] = run.metadata['deadlineAt'] - 3601
        config = {**helper.roles['backend_engineer'], '_ax_delegation': {
            'parent_session_id': helper.parent, 'root_session_id': helper.parent,
            'lineage': ['tech_lead', 'backend_engineer'],
            'implementationDeadlineAt': run.metadata['implementationDeadlineAt']}}
        job = Job('d' * 16, config, 'prepare change', run, run.deadline)
        job.canceled.set()
        helper.service.jobs[job.session_id] = job
        helper.service._execute(job)
        saved = helper.store.get(job.session_id)['roleConfig']['_ax_delegation']
        assert saved['state'] == 'TASK_STATE_CANCELED'
        assert saved['resumable'] is False
        assert helper.runner.chats == []


def test_queued_resume_keeps_previous_checkpoint_evidence_when_window_expires(helper):
    with workflow(helper) as run:
        run.metadata['implementationDeadlineAt'] = run.metadata['deadlineAt'] - 3601
        config = {**helper.roles['backend_engineer'], '_ax_delegation': {
            'parent_session_id': helper.parent, 'root_session_id': helper.parent,
            'lineage': ['tech_lead', 'backend_engineer'],
            'implementationDeadlineAt': run.metadata['implementationDeadlineAt']}}
        prior = {'status': 'partial', 'revision': 'a' * 40, 'evidence': ['patch and tests saved'],
                 'checks': [{'kind': 'checks', 'status': 'passed', 'revision': 'a' * 40}],
                 'artifacts': [{'name': 'fix.patch', 'content': 'saved patch'}],
                 'errors': ['QA not completed'], 'remaining_work': ['Verify the specific F1 correction']}
        checkpoint = {'outcome': prior, 'messages': [{'role': 'tool', 'content': 'tests passed'}]}
        chat = helper.runner.start_chat('d' * 16, config)
        chat.files['fix.patch'] = 'saved patch'
        chat.get_checkpoint = lambda: checkpoint
        job = Job('d' * 16, config, 'continue fixes', run, run.deadline, chat=chat)
        helper.service.jobs[job.session_id] = job
        helper.service._execute(job)
        saved = helper.store.get(job.session_id)['roleConfig']['_ax_delegation']
        for key in ('evidence', 'errors', 'revision', 'checks', 'artifacts'):
            assert saved['outcome'][key] == prior[key]
        assert prior['remaining_work'][0] in saved['outcome']['remaining_work']
        assert saved['outcome']['reason'] == 'verification_reserve'
        assert saved['resumable'] is True
        assert helper.service.retained[job.session_id][0].files['fix.patch'] == 'saved patch'


def test_expired_window_keeps_a_repeated_next_step_once(helper):
    with workflow(helper) as run:
        run.metadata['implementationDeadlineAt'] = run.metadata['deadlineAt'] - 3601
        config = {**helper.roles['backend_engineer'], '_ax_delegation': {
            'parent_session_id': helper.parent, 'root_session_id': helper.parent,
            'lineage': ['tech_lead', 'backend_engineer'],
            'implementationDeadlineAt': run.metadata['implementationDeadlineAt']}}
        step = 'Arrange review and QA of available artifacts before requesting corrective implementation'
        prior = {'status': 'partial', 'evidence': ['patch and tests saved'], 'errors': [],
                 'remaining_work': [step]}
        checkpoint = {'outcome': prior, 'messages': [{'role': 'tool', 'content': 'tests passed'}]}
        chat = helper.runner.start_chat('f' * 16, config)
        chat.get_checkpoint = lambda: checkpoint
        job = Job('f' * 16, config, 'continue fixes', run, run.deadline, chat=chat)
        helper.service.jobs[job.session_id] = job
        helper.service._execute(job)
        saved = helper.store.get(job.session_id)['roleConfig']['_ax_delegation']
        assert saved['outcome']['reason'] == 'verification_reserve'
        assert saved['outcome']['remaining_work'] == [step]

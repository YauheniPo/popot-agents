"""Initial implementation yields time for verification without expiring its sandbox."""
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from popot_agents.orchestrator.workflow import WorkflowRun
from popot_agents.runtime_config import RUNTIME
from popot_agents.workflow_config import validate_policy
from tests import test_workflow_execution as execution
from tests import test_agent_reliability as reliability


POLICY = {**execution.POLICY, 'total_seconds': 3600, 'turn_seconds': 900,
          'verification_reserve_seconds': 900}


def test_runtime_allocates_fifteen_minutes_within_the_existing_hour():
    assert RUNTIME['workflow']['total_seconds'] == 3600
    assert RUNTIME['workflow']['verification_reserve_seconds'] == 900
    assert RUNTIME['model']['final_answer_reserve_seconds'] == 120
    assert validate_policy(POLICY) == POLICY


@pytest.mark.parametrize('reserve', [0, -1, True, 1.5])
def test_verification_reserve_requires_positive_integer(reserve):
    with pytest.raises(ValueError):
        validate_policy({**POLICY, 'verification_reserve_seconds': reserve})


@pytest.mark.parametrize('duration, reserve', [(3600, 900), (600, 150)])
def test_absolute_implementation_cutoff_survives_restore(duration, reserve):
    policy = {**POLICY, 'total_seconds': duration, 'turn_seconds': min(duration, 900)}
    with patch('time.time', return_value=1000), patch('time.monotonic', return_value=100):
        first = WorkflowRun(policy, 'prepare PR')
    assert first.meta['implementationDeadlineAt'] == 1000 + duration - reserve
    assert first.meta['verificationReserveSeconds'] == reserve
    with patch('time.time', return_value=1100), patch('time.monotonic', return_value=200):
        restored = WorkflowRun(policy, '', first.meta)
    assert restored.meta['implementationDeadlineAt'] == first.meta['implementationDeadlineAt']
    assert restored.deadline == first.deadline


def test_legacy_run_is_not_given_a_new_cutoff_during_restore():
    old = WorkflowRun(execution.POLICY, 'old task')
    restored = WorkflowRun(POLICY, '', old.meta)
    assert 'implementationDeadlineAt' not in restored.meta


def test_initial_child_yields_checkpoint_at_soft_boundary_without_expiring(tmp_path):
    context = execution.execution('child', tmp_path, [(execution.report('partial'), execution.CHECKPOINT)])
    remaining = [200.0]
    context.service.implementation_remaining = lambda _sid: remaining[0]
    send = context.worker.send
    timeouts = []
    def limited_send(message, *, timeout_seconds):
        timeouts.append(timeout_seconds)
        result = send(message)
        remaining[0] = 20.0
        return result
    context.worker.send = limited_send
    deadline = context.job.deadline
    result = context.invoke()
    assert timeouts == [60]
    assert result['outcome']['status'] == 'partial'
    assert result['outcome']['reason'] == 'verification_reserve'
    assert result['outcome']['evidence'] == ['publication receipt']
    assert context.job.config['_ax_delegation']['workflow']['checkpoint'] == execution.CHECKPOINT
    assert context.job.deadline == deadline
    assert context.worker.closes == 0


def test_child_send_cannot_use_the_reserved_verification_window(tmp_path):
    context = execution.execution('child', tmp_path, [(execution.report('completed'), {})])
    context.service.implementation_remaining = lambda _sid: 50.0
    send = context.worker.send
    timeouts = []
    def limited_send(message, *, timeout_seconds):
        timeouts.append(timeout_seconds)
        return send(message)
    context.worker.send = limited_send
    assert context.invoke()['outcome']['status'] == 'completed'
    assert timeouts == [50.0]


def test_expired_soft_budget_does_not_send_or_close_retained_child(tmp_path):
    context = execution.execution('child', tmp_path, [])
    context.service.implementation_remaining = lambda _sid: 0.0
    context.workflow.meta['outcome'] = execution.report('partial')['outcome']
    context.worker.checkpoint = execution.CHECKPOINT
    result = context.invoke()
    assert result['outcome']['status'] == 'partial'
    assert result['outcome']['reason'] == 'verification_reserve'
    assert context.worker.calls == []
    assert context.worker.closes == 0
    assert context.job.config['_ax_delegation']['workflow']['checkpoint'] == execution.CHECKPOINT


def test_soft_checkpoint_keeps_prior_evidence_and_checks_on_retained_resume(tmp_path):
    context = execution.execution('child', tmp_path, [])
    context.service.implementation_remaining = lambda _sid: 0.0
    previous = {**execution.report('partial')['outcome'], 'revision': 'a' * 40,
                'checks': [{'command': 'pytest', 'summary': '895 passed'}]}
    context.worker.checkpoint = {**execution.CHECKPOINT, 'outcome': previous}
    result = context.invoke()
    for key in ('evidence', 'checks', 'revision', 'remaining_work'):
        assert result['outcome'][key] == previous[key]


def test_root_switches_to_verification_without_spending_its_last_coordination_turn(tmp_path):
    context = execution.execution('root', tmp_path,
        [(execution.report('partial'), execution.CHECKPOINT), (execution.report('completed'), {})], max_turns=1)
    remaining = [50.0]
    context.delegation = SimpleNamespace(workflow=lambda *_args, **_kwargs: nullcontext(),
        implementation_remaining=lambda _sid: remaining[0], await_children=lambda *_args: False)
    send = context.worker.send
    timeouts = []
    def limited_send(message, *, timeout_seconds):
        timeouts.append(timeout_seconds)
        remaining[0] = 0.0
        return send(message)
    context.worker.send = limited_send
    result = context.invoke()
    assert result['outcome']['status'] == 'completed'
    assert timeouts == [50.0, 60]
    assert context.workflow.meta['turns'] == 1
    assert context.workflow.meta['verificationPhaseStarted'] is True


def test_cancellation_is_not_replaced_by_a_verification_checkpoint(tmp_path):
    context = execution.execution('child', tmp_path, [])
    context.service.implementation_remaining = lambda _sid: 0.0
    context.job.canceled.set()
    with pytest.raises(TimeoutError):
        context.invoke()
    assert context.worker.calls == []


def test_soft_boundary_joins_pending_children_before_parking_the_parent():
    run = WorkflowRun(POLICY, 'implement and verify')
    run.begin()
    result = execution.report('partial', reason='child_pending')
    checkpoint = {'messages': [], 'failed_delegations': [[['analyst', 'inspect'],
                  {'session_id': 'b' * 16, 'pending': True}]]}
    terminal = run.record(result, checkpoint, yield_for_verification=True)
    assert terminal is None
    assert run.meta['awaiting_children'] is True
    assert run.meta['outcome']['reason'] == 'child_pending'


def test_soft_boundary_drains_remaining_children_after_wait_any_without_another_send(tmp_path):
    checkpoint = {'messages': [], 'failed_delegations': [
        [['analyst', sid], {'session_id': sid * 16, 'pending': True}] for sid in ('b', 'c')]}
    context = execution.execution('child', tmp_path,
        [(execution.report('partial', reason='child_pending'), checkpoint)])
    remaining = [50.0]
    context.service.implementation_remaining = lambda _sid: remaining[0]
    send = context.worker.send
    timeouts = []
    waits = []
    def limited_send(message, *, timeout_seconds):
        timeouts.append(timeout_seconds)
        remaining[0] = 0.0
        return send(message)
    def await_children(*_args):
        waits.append(True)
        context.worker.checkpoint['failed_delegations'].pop()
    context.worker.send = limited_send
    context.service.await_children = await_children
    result = context.invoke()
    assert result['outcome']['reason'] == 'verification_reserve'
    assert timeouts == [50.0]
    assert len(waits) == 2
    assert context.worker.checkpoint['failed_delegations'] == []


def test_worker_receives_server_budget_on_every_turn():
    helper = reliability.AgentReliabilityTests()
    budget = {'workflow_seconds_remaining': 880, 'implementation_seconds_remaining': 0,
              'verification_reserve_seconds': 900, 'implementation_limited': True,
              'verification_started': False}
    _, _, bodies = helper.run_sequence([helper.result()], run_context={'budget': budget})
    messages = [m['content'] for m in bodies[0]['messages'] if m['role'] == 'system']
    assert any(json.dumps(budget, ensure_ascii=False) in content for content in messages)

"""Coverage for the remaining delegation, AX suspension and child budget branches."""
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from ax_local.api import delegation
from ax_local.api.ax_runner import AxChat
from ax_local.api.delegation import DelegationService
from popot_agents.orchestrator.main import AgentConfigurationError, AgentRunError
from tests.test_workflow_execution import SESSION, execution


def test_configuration_failure_is_classified_as_worker_configuration():
    assert delegation._failure_classification(AgentConfigurationError('missing model')) == (
        'worker_configuration_failed', 'AX worker configuration failed')


def test_unreachable_model_endpoint_is_a_response_failure():
    assert delegation._run_error_detail('model endpoint is unreachable') == (
        'model_response_failed', 'model endpoint is unreachable')


def test_retry_suspend_records_the_retry_error():
    chat = AxChat.__new__(AxChat)

    def refuse(*_args, **_options):
        raise subprocess.TimeoutExpired('ax', 1)

    chat.container_name = 'fixture-task'
    chat.runner = SimpleNamespace(snapshot_mode='copy', _run=refuse)
    diagnostic = {}
    retry_at, delay = chat._retry_suspend(lambda limit: 10, diagnostic, 1)
    assert diagnostic['retry_error_type'] == 'TimeoutExpired'
    assert retry_at > time.monotonic() - 1
    assert delay == 2


def test_release_admission_rereaises_resize_failure_for_an_open_pool():
    service = object.__new__(DelegationService)
    job = SimpleNamespace(session_id='a' * 16)
    job.run = SimpleNamespace(admitted={job.session_id: job},
                              pool=SimpleNamespace(closed=False),
                              lane_condition=threading.Condition())
    service._resize_reservation = lambda *_args: (_ for _ in ()).throw(AgentRunError('resize failed'))
    with pytest.raises(AgentRunError, match='resize failed'):
        service._release_admission(job)


def test_await_children_rejects_a_mismatched_child_role(tmp_path):
    from popot_agents.orchestrator.session_store import SessionStore

    child = 'b' * 16
    parent = 'c' * 16
    store = SessionStore(tmp_path)
    store.save(child, 'child', [], 'backend_engineer',
               {'_ax_delegation': {'parent_session_id': parent,
                                   'state': 'TASK_STATE_COMPLETED',
                                   'outcome': {'status': 'completed'}}})
    service = DelegationService({}, {}, {}, store, 'http://fixture.invalid')
    checkpoint = {'await_tasks': {'session_ids': [child], 'wait_for': 'any'},
                  'failed_delegations': [[('qa_engineer', 'alias'),
                                          {'pending': True, 'session_id': child}]]}
    worker = SimpleNamespace(get_checkpoint=lambda: checkpoint, runner=None)
    with pytest.raises(AgentRunError, match='Pending child role mismatch'):
        service.await_children(parent, worker, [], time.monotonic() + 5)


def test_child_begin_stops_when_the_workflow_budget_is_exhausted(tmp_path):
    context = execution('child', tmp_path, [])
    context.workflow.deadline = time.monotonic()
    result = context.invoke()
    assert result['outcome']['reason'] == 'workflow_deadline_exhausted'
    assert context.worker.calls == []


def test_delegate_task_rejects_a_workflow_without_delegation(capfd):
    import io
    import json
    import os
    from unittest.mock import patch

    from popot_agents.worker import http_harness

    tools = {'delegate_task': {
        'schema': {'type': 'function', 'function': {
            'name': 'delegate_task', 'parameters': {'type': 'object', 'properties': {}}}},
        'call': lambda *_a, **_k: '{}'}}
    responses = [
        {'tool_calls': [{'id': 'd1', 'type': 'function', 'function': {
            'name': 'delegate_task',
            'arguments': json.dumps({'role': 'backend_engineer', 'task': 'do work'})}}]},
        {'content': json.dumps({'status': 'completed', 'answer': 'done', 'evidence': ['x'],
                                'remaining_work': [], 'errors': []})}]

    def respond(_call, **_options):
        message = responses.pop(0) if len(responses) > 1 else responses[0]
        return io.BytesIO(json.dumps({'choices': [{'message': message}]}).encode())

    state = {}
    with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1', 'HARNESS_MODEL': 'fake'},
                    clear=False), \
            patch('popot_agents.worker.http_harness.request.urlopen', side_effect=respond), \
            patch('popot_agents.worker.http_harness.execute_tool', return_value='{}'):
        answer = http_harness.run_http(
            'delegate and verify', {'tools': [], 'max_tool_rounds': 1},
            extra_tools=tools, state=state, run_context={'can_delegate': False})
    trace = capfd.readouterr().err
    assert 'Delegation is unavailable in this workflow context' in trace
    assert '"error_type": "ToolInputError"' in trace
    assert 'done' in answer

"""Coverage for the remaining new-code branches of the verification lifecycle work."""
import io
import json
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from popot_agents import work_evidence as evidence
from popot_agents.orchestrator.workflow import WorkflowRun
from popot_agents.orchestrator.workflow_execution import _RootExecution
from popot_agents.runtime_config import validate_workflow_policy
from popot_agents.orchestrator.session_store import SessionStore
from popot_agents.worker import file_worker, final_result
from tests.test_workflow_execution import SESSION, Worker, execution, report

POLICY = {'max_turns': 2, 'total_seconds': 1, 'turn_seconds': 1,
          'max_no_progress': 1, 'max_concurrent': 1}


class DeadWorker:
    container_name = 'fixture-worker'

    def __init__(self, closes=0):
        self.closes = closes

    def is_alive(self):
        return self.closes == 0

    def close(self):
        self.closes += 1


def test_stdin_override_skips_non_stdin_descriptors():
    assert evidence._stdin_overridden('here 3<z cat') is False


def test_detach_shadowed_script_keeps_scanning_after_other_consumer():
    bodies = [{'consumer': (1, 1), 'script': True}, {'consumer': (2, 2), 'script': True}]
    evidence._detach_shadowed_script(bodies, (2, 2))
    assert bodies[0]['script'] is True
    assert bodies[1]['script'] is False


def test_spec_skip_rejects_nested_parentheses():
    assert evidence._spec_skip('﹣ x (f (g)) # why') is None


def test_observation_without_exit_code_keeps_revision_and_skips():
    receipt = final_result._observation_receipt(
        {'kind': 'checks', 'tool': 'pytest', 'callId': 'c1', 'status': 'passed',
         'summary': '12 passed', 'revision': 'a' * 40, 'outputSha256': 'b' * 64,
         'skipCount': 2}, 400)
    assert 'exit_code=' not in receipt
    assert 'skipped=2' in receipt
    assert 'revision=' + 'a' * 40 in receipt


def test_failed_finish_without_checkpoint_leaves_no_checkpoint():
    workflow = WorkflowRun({**POLICY, 'total_seconds': 60}, 'publish once')
    result = workflow._finish({'answer': 'failed'}, 'failed', None)
    assert result == {'answer': 'failed'}
    assert 'checkpoint' not in workflow.meta


def test_await_children_stops_when_no_time_remains():
    workflow = WorkflowRun({**POLICY, 'total_seconds': 1}, 'publish once')
    outcome = workflow._await_children({'answer': 'waiting for children'})
    assert outcome['outcome']['reason'] == 'workflow_deadline_exhausted'
    assert workflow.meta['state'] == 'blocked'


@pytest.mark.parametrize('exhausted,reason', [
    (False, 'workflow_turn_limit'),
    (True, 'workflow_deadline_exhausted')])
def test_root_begin_stops_when_budget_is_exhausted(tmp_path, exhausted, reason):
    workflow = WorkflowRun({**POLICY, 'total_seconds': 60}, 'publish once')
    workflow.meta['turns'] = 2
    workflow.meta['awaiting_children'] = False
    if exhausted:
        workflow.deadline = time.monotonic()
    store = SessionStore(tmp_path)
    record = SimpleNamespace(agent='root', role='root', messages=[], worker=DeadWorker(),
                             role_config={})
    server = SimpleNamespace(store=store, live={SESSION: record}, live_lock=threading.RLock())
    result = _RootExecution(server, SESSION, record, 'publish once', workflow, {}, None).begin()
    assert result['outcome']['reason'] == reason
    assert workflow.meta['state'] == 'blocked'


def test_root_begin_stops_on_turn_limit_when_time_remains(tmp_path):
    workflow = WorkflowRun({**POLICY, 'total_seconds': 60, 'max_turns': 1}, 'publish once')
    workflow.meta['turns'] = 1
    store = SessionStore(tmp_path)
    record = SimpleNamespace(agent='root', role='root', messages=[], worker=DeadWorker(),
                             role_config={})
    server = SimpleNamespace(store=store, live={SESSION: record}, live_lock=threading.RLock())
    result = _RootExecution(server, SESSION, record, 'publish once', workflow, {}, None).begin()
    assert result['outcome']['reason'] == 'workflow_turn_limit'


def test_check_summary_without_pass_marker_records_observation():
    observation = evidence.observe_tool_result(
        'bash', {'command': 'python -m pytest -q'}, 'exit_code=0\nRan 2 tests', 'c1')
    assert observation['kind'] == 'checks'
    assert observation['status'] == 'observed'
    assert observation['summary'] == 'Ran 2 tests'


def test_workflow_policy_rejects_unknown_fields():
    with pytest.raises(ValueError):
        validate_workflow_policy({**POLICY, 'bogus': 1}, 30)


def test_export_artifacts_reports_artifact_error(monkeypatch, capsys):
    from popot_agents import artifacts

    def refuse(_root):
        raise artifacts.ArtifactError('count_limit', 3)

    monkeypatch.setattr(artifacts, 'collect_artifacts', refuse)
    monkeypatch.setattr(sys, 'argv', ['file_worker.py', 'export_artifacts'])
    monkeypatch.setattr(sys, 'stdin', io.StringIO('{}'))
    with pytest.raises(SystemExit) as raised:
        file_worker.main()
    assert raised.value.code == 2
    assert json.loads(capsys.readouterr().out) == {'artifact_error': 'count_limit', 'minimum': 3}


def test_continuation_context_limit_stops_workflow(tmp_path):
    context = execution('root', tmp_path, [(report('partial'), {'messages': []})])

    def refuse(**_options):
        raise ValueError('conversation exceeds context window')

    context.workflow.continuation = refuse
    result = context.invoke()
    assert result['outcome']['reason'] == 'workflow_context_limit'
    assert len(context.worker.calls) == 1


def test_uncertain_send_discards_a_dead_root_worker(tmp_path):
    from popot_agents.orchestrator.main import AgentRunError

    context = execution('root', tmp_path, [(AgentRunError('response lost'), None)])
    context.worker.closes = 1
    result = context.invoke()
    assert result['outcome']['reason'] == 'workflow_execution_uncertain'
    assert context.worker.closes == 2


def test_timeout_without_workflow_reraises(tmp_path):
    from popot_agents.orchestrator.workflow_execution import run_root

    store = SessionStore(tmp_path)
    worker = Worker(store, [(subprocess.TimeoutExpired('fixture', 1), None)])
    record = SimpleNamespace(agent='root', role='root', messages=[], worker=worker,
                             role_config={})
    server = SimpleNamespace(store=store, live={SESSION: record}, live_lock=threading.RLock())
    with pytest.raises(subprocess.TimeoutExpired):
        run_root(server, SESSION, record, 'publish once', None, role_config={}, delegation=None)
    assert worker.closes == 1


def test_partial_outcome_without_checkpoint_stops_the_workflow():
    workflow = WorkflowRun({**POLICY, 'total_seconds': 60}, 'publish once')
    result = workflow.record({'outcome': {'status': 'partial', 'evidence': ['patch saved'],
                                          'remaining_work': ['finish review'], 'errors': []}},
                            None)
    assert result['outcome']['reason'] == 'workflow_checkpoint_unavailable'
    assert workflow.meta['state'] == 'blocked'

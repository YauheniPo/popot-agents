"""Keep completed verification separate from acceptance and preserve audit evidence."""
import hashlib
import json
from unittest.mock import patch

import pytest

from popot_agents.worker.final_result import parse_result, retained_receipts
from popot_agents.worker.verification import verified_roles
from popot_agents.orchestrator.main import AgentRunError
from tests import test_agent_reliability as reliability
from tests.test_harness_loops import tool_call
from tests.test_workflow_execution import execution, report, SESSION


REVISION = 'a' * 40


def test_native_loaded_file_completes_implementation_readback_without_repeating_requests():
    helper = reliability.AgentReliabilityTests()
    name = 'mcp__github__get_file_contents'
    args = {'owner': 'example', 'repo': 'repo', 'path': 'app.py', 'ref': REVISION}
    output = (f'successfully downloaded text file (SHA: {"b" * 40})\n'
              f'[repo://example/repo/sha/{REVISION}/contents/app.py]\nprint("hello")\n')
    discovered = {name: {'server': 'github', 'native_name': 'get_file_contents',
        'schema': {'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object'}}}}}
    result = {**json.loads(helper.result()['content']), 'revision': REVISION, 'findings': []}
    with patch('popot_agents.worker.http_harness.mcp_client.discover_tools', return_value=discovered), \
         patch('popot_agents.worker.http_harness.mcp_client.call_tool', return_value=output) as native:
        _, state, _ = helper.run_sequence([
            {'tool_calls': [tool_call('read', name, args)]}, {'content': json.dumps(result)}],
            role={'name': 'backend_engineer', 'tools': [], 'mcpServers': {'github': {}}},
            run_context={'run_id': 'b' * 32, 'authorization': ['Read https://github.com/example/repo/pull/94']})
    assert state['outcome']['status'] == 'completed'
    assert state['outcome']['revision'] == REVISION
    assert native.call_count == 1


def test_open_in_scope_minor_finding_cannot_pass_verification_or_acceptance():
    finding = {'description': 'commit-graph write is blocked by the new guard',
               'scope': 'in_scope', 'status': 'open'}
    child = {'role': 'code_reviewer', 'outcome': {'status': 'completed',
        'verification_status': 'passed', 'revision': REVISION,
        'evidence': ['reproduced'], 'findings': [finding]}}
    assert verified_roles([child], REVISION) == set()


def test_scoped_review_normalizes_pass_with_an_open_finding_to_failed_verdict():
    helper = reliability.AgentReliabilityTests()
    report = json.loads(helper.result()['content'])
    report.update(verification_status='passed', findings=[{
        'description': 'literal echo is incorrectly blocked', 'scope': 'in_scope', 'status': 'open'}])
    _, state, _ = helper.run_sequence([{'content': json.dumps(report)}],
        role={'name': 'code_reviewer', 'tools': []})
    assert state['outcome']['status'] == 'completed'
    assert state['outcome']['verification_status'] == 'failed'
    assert state['outcome']['findings'] == report['findings']


def test_coordinator_cannot_dismiss_an_open_minor_finding_as_optional():
    helper = reliability.AgentReliabilityTests()
    children = {role: {'sessionId': str(i) * 16, 'role': role, 'outcome': {
        'status': 'completed', 'verification_status': 'passed', 'revision': REVISION,
        'evidence': ['checked'], 'findings': []}}
        for i, role in enumerate(('backend_engineer', 'code_reviewer', 'qa_engineer'))}
    children['code_reviewer']['outcome']['findings'] = [{
        'description': 'minor: commit-graph write is incorrectly blocked',
        'scope': 'in_scope', 'status': 'open'}]
    answer, state, _ = helper.run_sequence([helper.result(answer='Ready; optional follow-up')],
        state={'messages': [{'role': 'user', 'content': 'Fix all repository defects'}],
               'outcome': {'status': 'partial'}, 'completed_delegations': children},
        role={'name': 'planner', '_workflow_coordinator': True, 'tools': []})
    assert state['outcome']['status'] == 'partial'
    assert 'Ready; optional follow-up' not in answer
    assert 'findings' in ' '.join(state['outcome']['remaining_work'])


def test_out_of_scope_findings_require_a_concrete_rationale():
    report = json.loads(reliability.AgentReliabilityTests.result()['content'])
    report['findings'] = [{'description': 'documented variable expansion boundary',
        'scope': 'out_of_scope', 'status': 'open'}]
    with pytest.raises(ValueError, match='findings'):
        parse_result(json.dumps(report))


def test_coordinated_passing_review_requires_an_explicit_findings_assessment():
    helper = reliability.AgentReliabilityTests()
    report = {**json.loads(helper.result()['content']), 'verification_status': 'passed'}
    _, state, _ = helper.run_sequence([{'content': json.dumps(report)}],
        role={'name': 'code_reviewer', 'tools': []},
        run_context={'run_id': 'b' * 32, 'authorization': ['Review the change']})
    assert state['outcome']['status'] == 'partial'
    assert state['outcome']['reason'] == 'verification_findings_missing'


@pytest.mark.parametrize('field,value', [('scope', []), ('status', {}), ('description', None)])
def test_malformed_finding_is_a_recoverable_format_error(field, value):
    report = json.loads(reliability.AgentReliabilityTests.result()['content'])
    finding = {'description': 'a real defect', 'scope': 'in_scope', 'status': 'open', field: value}
    report['findings'] = [finding]
    with pytest.raises(ValueError, match='findings'):
        parse_result(json.dumps(report))


def test_known_implementation_revision_shares_checks_without_requiring_repeated_sha_argument():
    from unittest.mock import Mock
    from tests.test_harness_loops import tool_call
    helper = reliability.AgentReliabilityTests()
    receipt = {'kind': 'checks', 'source': 'tool_result', 'status': 'passed',
               'revision': REVISION, 'exitCode': 0, 'summary': '921 passed'}
    child = {'sessionId': 'a' * 16, 'role': 'backend_engineer', 'outcome': {
        'status': 'completed', 'revision': REVISION, 'evidence': ['implemented'], 'checks': [receipt]}}
    delegate = Mock(return_value=json.dumps({'sessionId': 'b' * 16, 'role': 'qa_engineer',
        'state': 'TASK_STATE_COMPLETED', 'answer': 'QA complete', 'outcome': {
            'status': 'completed', 'revision': REVISION, 'verification_status': 'passed',
            'evidence': ['checks reused'], 'findings': []}}))
    helper.run_sequence([{'tool_calls': [tool_call('qa', 'delegate_task', {
        'role': 'qa_engineer', 'task': 'Verify the current implementation'})]}, helper.result('partial')],
        delegate=delegate, state={'messages': [{'role': 'user', 'content': 'Fix the repository'}],
        'outcome': {'status': 'partial'}, 'completed_delegations': {child['sessionId']: child}},
        role={'name': 'planner', '_workflow_coordinator': True, 'tools': []})
    task = delegate.call_args.args[0]
    assert task.get('_scope_revision') == REVISION
    assert '921 passed' in task['task']


@pytest.mark.parametrize('finding', [
    {'description': 'fixed regression', 'scope': 'in_scope', 'status': 'resolved'},
    {'description': 'documented dynamic-shell boundary', 'scope': 'out_of_scope',
     'status': 'open', 'rationale': 'The unchanged role contract explicitly excludes shell interpretation'},
])
def test_resolved_or_justified_out_of_scope_findings_do_not_block_review(finding):
    helper = reliability.AgentReliabilityTests()
    report = {**json.loads(helper.result()['content']), 'verification_status': 'passed', 'findings': [finding]}
    _, state, _ = helper.run_sequence([{'content': json.dumps(report)}],
        role={'name': 'code_reviewer', 'tools': []},
        run_context={'run_id': 'b' * 32, 'authorization': ['Review the change']})
    assert state['outcome']['status'] == 'completed'
    assert state['outcome']['verification_status'] == 'passed'


@pytest.mark.parametrize('status', ['partial', 'blocked'])
def test_child_checkpoint_survives_database_roundtrip(tmp_path, status):
    checkpoint = {'messages': [{'role': 'assistant', 'tool_calls': [
        tool_call('denied', 'bash', {'command': 'git push origin feature'})]}],
        'tool_evidence': [{'kind': 'checks', 'summary': '895 passed', 'callId': 'tests'}]}
    context = execution('child', tmp_path, [(report(status), checkpoint)], max_turns=1)
    context.invoke()
    context.service._save(context.job, 'TASK_STATE_COMPLETED', status)
    saved = context.store.get(SESSION)['roleConfig']['_ax_delegation']['workflow']
    assert saved['checkpoint'] == checkpoint


def test_child_resume_transport_failure_keeps_previous_persisted_checkpoint(tmp_path):
    checkpoint = {'messages': [{'role': 'tool', 'tool_call_id': 'suite', 'content': '895 passed'}]}
    context = execution('child', tmp_path, [(AgentRunError('resume transport failed'), None)])
    context.job.config['_ax_delegation']['workflow'] = {
        'state': 'blocked', 'turns': 1, 'checkpoint': checkpoint}
    context.worker.checkpoint = checkpoint
    with pytest.raises(AgentRunError, match='resume transport failed'):
        context.invoke()
    saved = context.store.get(SESSION)['roleConfig']['_ax_delegation']['workflow']
    assert saved['checkpoint'] == checkpoint


def test_retained_check_receipts_survive_later_noise_without_inventing_revision():
    receipt = {'source': 'tool_result', 'callId': 'suite', 'tool': 'bash',
               'kind': 'checks', 'status': 'passed', 'summary': '895 passed',
               'exitCode': 0, 'outputSha256': 'b' * 64, 'observedAt': '2026-11-07T07:27:11Z'}
    messages = [{'role': 'tool', 'tool_call_id': f'read-{i}', 'content': 'file content'} for i in range(20)]
    receipts = retained_receipts(messages, {}, [receipt])
    assert any('895 passed' in item and 'suite' in item for item in receipts)
    assert any('revision=unverified' in item for item in receipts)


def test_blocked_qa_keeps_checks_and_rejected_command_identity():
    helper = reliability.AgentReliabilityTests()
    command = 'git push origin feature'
    replies = [{'tool_calls': [tool_call('suite', 'bash', {'command': 'python3 -m pytest -q'})]}]
    replies += [{'tool_calls': [tool_call(f'read-{i}', 'read_file', {'path': 'README.md'})]}
                for i in range(14)]
    replies += [{'tool_calls': [tool_call(f'blocked-{i}', 'bash', {'command': command})]}
                for i in range(2)]
    def execute(name, *_args, **_kwargs):
        return 'exit_code=0\n895 passed, 32 skipped in 4.0s' if name == 'bash' else 'file content'
    with patch('popot_agents.worker.http_harness.execute_tool', side_effect=execute), \
         patch('popot_agents.worker.http_harness._trace') as trace:
        _, state, _ = helper.run_sequence(replies, role={'name': 'qa_engineer', 'tools': ['bash', 'read_file']},
            run_context={'session_id': 'b' * 16, 'publication_owner': 'c' * 16})
    assert state['outcome']['reason'] == 'non_retryable_tool_input'
    assert any('895 passed' in item for item in state['outcome']['evidence'])
    assert state['outcome']['checks'][0]['summary'] == '895 passed'
    assert 'revision' not in state['outcome']['checks'][0]
    rejected = [call.kwargs for call in trace.call_args_list
                if call.args[0] == 'tool_error' and call.kwargs.get('call_id') == 'blocked-1'][0]
    assert rejected['arguments']['command_sha256'] == hashlib.sha256(command.encode()).hexdigest()


def test_failed_verification_is_a_completed_subtask_but_not_an_acceptance_receipt():
    child = {'role': 'code_reviewer', 'outcome': {'status': 'completed', 'verification_status': 'failed',
             'revision': REVISION, 'evidence': ['F1: commit guard bypass; implementation owner must fix']}}
    assert verified_roles([child], REVISION) == set()
    child['outcome']['verification_status'] = 'passed'
    assert verified_roles([child], REVISION) == {'code_reviewer'}


def test_newer_failed_review_supersedes_earlier_pass_at_same_revision():
    earlier = {'role': 'code_reviewer', 'reportedAt': '2026-11-07T07:00:00Z', 'outcome': {
        'status': 'completed', 'verification_status': 'passed', 'revision': REVISION, 'evidence': ['checked']}}
    latest = {**earlier, 'reportedAt': '2026-11-07T07:01:00Z',
              'outcome': {**earlier['outcome'], 'verification_status': 'failed'}}
    assert verified_roles([latest, earlier], REVISION) == set()


def test_coordinator_cannot_accept_completed_negative_review():
    helper = reliability.AgentReliabilityTests()
    children = {}
    for index, role in enumerate(['backend_engineer', 'code_reviewer', 'qa_engineer']):
        sid = str(index) * 16
        children[sid] = {'sessionId': sid, 'role': role, 'outcome': {
            'status': 'completed', 'revision': REVISION, 'evidence': ['checked'],
            'verification_status': 'failed' if role == 'code_reviewer' else 'passed'}}
    _, state, _ = helper.run_sequence([helper.result()], state={
        'messages': [{'role': 'user', 'content': 'Fix and verify the repository'}],
        'outcome': {'status': 'partial'}, 'completed_delegations': children},
        role={'name': 'planner', '_workflow_coordinator': True, 'tools': []})
    assert state['outcome']['status'] == 'partial'
    assert state['outcome']['reason'] == 'workflow_validation_failed'
    assert 'findings' in ' '.join(state['outcome']['remaining_work'])


def test_harness_preserves_scoped_negative_verification():
    helper = reliability.AgentReliabilityTests()
    result = helper.result(answer='Review complete: F1 needs an implementation fix.')
    result['content'] = json.dumps({**json.loads(result['content']), 'verification_status': 'failed'})
    _, state, _ = helper.run_sequence([result], role={'name': 'code_reviewer', 'tools': []})
    assert state['outcome']['status'] == 'completed'
    assert state['outcome']['verification_status'] == 'failed'


@pytest.mark.parametrize('explicit_negative_report', [False, True])
def test_review_only_parent_must_acknowledge_negative_child_verdict(explicit_negative_report):
    helper = reliability.AgentReliabilityTests()
    result = helper.result(answer='Review delivered with F1 requiring an implementation fix.')
    if explicit_negative_report:
        result['content'] = json.dumps({**json.loads(result['content']), 'verification_status': 'failed'})
    child = {'sessionId': 'b' * 16, 'role': 'code_reviewer', 'outcome': {
        'status': 'completed', 'revision': REVISION, 'verification_status': 'failed', 'evidence': ['F1 confirmed']}}
    _, state, _ = helper.run_sequence([result], state={
        'messages': [{'role': 'user', 'content': 'Review and report findings'}],
        'outcome': {'status': 'partial'}, 'completed_delegations': {child['sessionId']: child}},
        role={'name': 'planner', '_workflow_coordinator': True, 'tools': []})
    assert state['outcome']['verification_status'] == 'failed'
    assert state['outcome']['status'] == ('completed' if explicit_negative_report else 'partial')


def test_invalid_verification_status_is_rejected():
    result = json.loads(reliability.AgentReliabilityTests.result()['content'])
    with pytest.raises(ValueError, match='invalid_verification_status'):
        parse_result(json.dumps({**result, 'verification_status': 'maybe'}))

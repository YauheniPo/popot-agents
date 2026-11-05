import json

from popot_agents.work_evidence import observe_tool_result

SHA = '882eea209bebdcea15141b875b12819072d4ca8d'


def bash(output, command, exit_code=0):
    return observe_tool_result('bash', {'command': command}, f'exit_code={exit_code}\n{output}', 'call-1')


def test_git_commit_is_observed_as_publication_pending_not_published():
    receipt = bash('[feat/async 882eea2] Add regression tests\n', 'git commit -m "Add regression tests"')
    assert receipt['kind'] == 'commit'
    assert receipt['status'] == 'observed'
    assert 'publication is separate' in receipt['summary']


def test_git_push_is_observed_as_publication():
    receipt = bash(' \t882eea2..15d3e77 HEAD -> feat/async\n', 'git push origin HEAD:feat/async')
    assert receipt['kind'] == 'publication'
    assert receipt['status'] == 'observed'


def test_revision_read_back_is_recorded_for_git_rev_parse():
    receipt = bash(f'{SHA}\n', 'git rev-parse HEAD')
    assert receipt['kind'] == 'revision'
    assert receipt['revision'] == SHA


def test_github_commit_read_uses_the_tool_payload_revision():
    receipt = observe_tool_result('github_get_commit', {}, json.dumps({'sha': SHA}), 'call-2')
    assert receipt['kind'] == 'revision'
    assert receipt['revision'] == SHA


def test_ci_failures_are_named_in_the_receipt():
    payload = {'check_runs': [{'name': 'CodeQL', 'conclusion': 'failure'},
                              {'name': 'tests', 'conclusion': 'success'}]}
    receipt = bash(json.dumps(payload), 'gh pr checks 37')
    assert receipt['kind'] == 'ci'
    assert receipt['status'] == 'failed'
    assert receipt['summary'] == 'CI failures: CodeQL'


def test_ci_pending_checks_are_reported_as_pending_names():
    payload = {'check_runs': [{'name': 'SonarQube Cloud Quality Gate', 'conclusion': None}]}
    receipt = bash(json.dumps(payload), 'gh pr checks 37')
    assert receipt['kind'] == 'ci'
    assert receipt['status'] == 'observed'
    assert 'CI pending: SonarQube Cloud Quality Gate' in receipt['summary']


def test_passing_ci_checks_are_recorded_as_passed():
    payload = {'check_runs': [{'name': 'tests', 'conclusion': 'success'},
                              {'name': 'docs', 'conclusion': 'skipped'}]}
    receipt = bash(json.dumps(payload), 'gh pr checks 37')
    assert receipt['kind'] == 'ci'
    assert receipt['status'] == 'passed'

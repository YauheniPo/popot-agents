"""Exercise the safe snapshot consumed by the real Holmes tool command."""

import io
import json
import subprocess
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest


def pod():
    return {
        'metadata': {'name': 'metrics', 'creationTimestamp': '2026-11-01T00:00:00Z'},
        'spec': {'containers': [
            {'name': 'metrics', 'resources': {'requests': {'cpu': '25m', 'memory': '32Mi'}},
             'env': [{'name': 'PASSWORD', 'value': 'private-env-value'}]},
            {'name': 'sidecar', 'command': ['private-command']}],
            'initContainers': [{'name': 'init'}]},
        'status': {'phase': 'Running',
            'conditions': [{'type': 'Ready', 'status': 'True',
                            'lastTransitionTime': '2026-11-10T00:00:00Z'}],
            'containerStatuses': [
                {'name': 'sidecar', 'ready': False, 'restartCount': 0,
                 'state': {'waiting': {'reason': 'ContainerCreating'}}},
                {'name': 'metrics', 'ready': True, 'restartCount': 118,
                 'state': {'running': {'startedAt': '2026-11-10T00:00:00Z'}},
                 'lastState': {'terminated': {'reason': 'Error', 'exitCode': 2,
                     'finishedAt': '2026-11-09T00:00:00Z', 'message': 'private-error'}}}],
            'initContainerStatuses': [{'name': 'init', 'ready': True, 'restartCount': 0,
                'state': {'terminated': {'reason': 'Completed', 'exitCode': 0}}}]}}


def test_cpu_units_exit_codes_and_historical_state_are_separate_and_safe():
    from ax_local.holmes.pod_evidence import snapshot
    result = snapshot({'items': [pod()]}, 'otel-system')
    assert result['source_type'] == 'observation'
    assert result['observed_at']
    row = result['pods'][0]
    assert row['phase'] == 'Running'
    assert row['conditions'][0]['status'] == 'True'
    metrics, sidecar, init = row['containers']
    assert metrics['name'] == 'metrics'
    assert metrics['ready'] is True
    assert metrics['restart_count'] == 118
    assert metrics['resources']['requests']['cpu'] == '25m'
    assert metrics['state'] == {'running': {'startedAt': '2026-11-10T00:00:00Z'}}
    assert metrics['previous_termination']['exitCode'] == 2
    assert type(metrics['previous_termination']['exitCode']) is int
    assert sidecar['ready'] is False
    assert sidecar['previous_termination'] is None
    assert init['type'] == 'init'
    assert init['state']['terminated']['exitCode'] == 0
    assert 'private-' not in json.dumps(result)


def test_snapshot_reports_missing_status_and_truncation():
    from ax_local.holmes.pod_evidence import MAX_PODS, MAX_CONTAINERS, snapshot
    item = {'metadata': {'name': 'pending'}, 'spec': {'containers': [
        {'name': str(i)} for i in range(MAX_CONTAINERS + 1)]}}
    result = snapshot({'items': [item] * (MAX_PODS + 1)}, 'otel-system')
    assert result['total_pods'] == MAX_PODS + 1
    assert 0 < len(result['pods']) <= MAX_PODS
    assert result['truncated'] is True
    row = result['pods'][0]
    assert row['containers_truncated'] is True
    assert len(row['containers']) == MAX_CONTAINERS
    assert row['containers'][0]['ready'] is None
    assert row['containers'][0]['restart_count'] is None
    small = {'metadata': {'name': 'pending'}, 'spec': {}}
    assert len(snapshot({'items': [small] * (MAX_PODS + 1)}, 'otel-system')['pods']) == MAX_PODS


def test_mounted_reader_uses_argument_list_and_propagates_kubectl_failures():
    from ax_local.holmes.pod_evidence import main
    with patch('sys.argv', ['pod_evidence.py', 'otel-system']), \
         patch('ax_local.holmes.pod_evidence.subprocess.run') as run, \
         redirect_stdout(io.StringIO()) as output:
        run.return_value.stdout = json.dumps({'items': [pod()]})
        main()
        assert json.loads(output.getvalue())['namespace'] == 'otel-system'
        assert run.call_args.args[0] == [
            'kubectl', '--request-timeout=20s', '-n', 'otel-system', 'get', 'pods', '-o', 'json']
        assert run.call_args.kwargs['check'] is True
        assert run.call_args.kwargs['timeout'] <= 30
        for error in (subprocess.CalledProcessError(1, 'kubectl'),
                      subprocess.TimeoutExpired('kubectl', 25)):
            run.side_effect = error
            with pytest.raises(type(error)):
                main()
    with patch('sys.argv', ['pod_evidence.py', 'otel-system; env']), \
         patch('ax_local.holmes.pod_evidence.subprocess.run') as run:
        with pytest.raises(ValueError):
            main()
        run.assert_not_called()


def test_snapshot_keeps_complete_json_within_the_context_budget():
    from ax_local.holmes.pod_evidence import MAX_OUTPUT_CHARS, snapshot
    item = pod()
    item['status']['containerStatuses'][0]['state']['waiting']['reason'] = 'x' * 2000
    result = snapshot({'items': [item] * 100}, 'otel-system')
    assert len(json.dumps(result)) <= MAX_OUTPUT_CHARS
    assert result['truncated'] is True
    assert result['total_pods'] == 100
    assert result['pods']

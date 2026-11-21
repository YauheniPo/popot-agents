"""Objective scoring must not trust the agent's completion claims."""
import base64
from contextlib import ExitStack
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from evals.baseline import evaluate, summarize


def artifact(name, data, *, binary=False):
    return {'name': name, 'content': base64.b64encode(data).decode() if binary else data.decode(),
            'sha256': hashlib.sha256(data).hexdigest(), **({'encoding': 'base64'} if binary else {})}


class BaselineTests(unittest.TestCase):
    def test_cli_defaults_come_from_json_and_explicit_overrides_are_frozen(self):
        from evals.baseline import load_config, main

        settings = load_config()
        settings.update(model='cloud/from-config', repetitions=3)
        for flags, model, repetitions in [([], 'cloud/from-config', 3),
                                          (['--model', 'cloud/from-cli', '--repetitions', '1'],
                                           'cloud/from-cli', 1)]:
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as tmp, \
                    patch('evals.baseline.SETTINGS', settings), \
                    patch('evals.baseline.start_server') as start, \
                    patch('evals.baseline.call', side_effect=KeyboardInterrupt):
                output = Path(tmp) / 'run'
                start.return_value = (Mock(), Mock(), Mock(), {'baseline': {}})
                with patch('sys.argv', ['baseline', '--output', str(output), *flags]), \
                        patch('builtins.print'), self.assertRaises(KeyboardInterrupt):
                    main()
                start.assert_called_once_with(output, model)
                frozen = json.loads((output / 'config.json').read_text())
                self.assertEqual(frozen['baseline']['repetitions'], repetitions)

    def test_eval_config_validates_model_repetitions_and_workflow_before_startup(self):
        from evals.baseline import ROOT, load_config

        config = json.loads((ROOT / 'evals/config.json').read_text())
        settings = config['x-baseline']
        settings.update(model='cloud/another-model', repetitions=3)
        settings['workflow'].update(total_seconds=400, turn_seconds=200)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text(json.dumps(config))
            self.assertEqual(load_config(path), settings)
            for key, value in [('model', ''), ('repetitions', True), ('repetitions', 0),
                               ('request_timeout_seconds', 100), ('inspection_timeout_seconds', -1)]:
                with self.subTest(key=key, value=value):
                    changed = {**config, 'x-baseline': {**settings, key: value}}
                    path.write_text(json.dumps(changed))
                    with self.assertRaises(ValueError):
                        load_config(path)
            settings['workflow']['turn_seconds'] = 500
            path.write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                load_config(path)

    def test_all_eval_role_snapshots_disable_memory_without_changing_production_roles(self):
        from evals.baseline import ROOT, load_config, start_server
        from popot_agents.worker.turn_memory import TurnMemory
        originals = {p: p.read_bytes() for p in (ROOT / 'config/roles').glob('*.json')}
        settings = load_config()
        settings['workflow'].update(total_seconds=400, turn_seconds=200, max_concurrent=2)
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            stack.enter_context(patch('evals.baseline.SETTINGS', settings))
            image = Path(tmp) / 'worker-image'
            image.write_text('worker@sha256:test')
            stack.enter_context(patch.dict('os.environ', {'AX_LOCAL_WORKER_IMAGE_FILE': str(image)}))
            for target in (
                'ax_local.api.server.configure_ax_server_tunnel',
                'ax_local.api.server.configure_delegation',
                'ax_local.api.server.kind_control_plane_host',
                'ax_local.api.provider_proxy.network_address_toward',
                'ax_local.api.provider_proxy.resolve_provider',
                'ax_local.api.provider_proxy.start_proxy',
                'ax_local.api.worker_pool.KubernetesWorkerPool',
                'ax_local.api.worker_pool.WorkerPoolController',
                'ax_local.api.ax_runner.AxAgentRunner',
                'ax_local.api.delegation.start_a2a_server',
                'popot_agents.orchestrator.main.create_server',
            ):
                stack.enter_context(patch(target))
            _, _, _, config = start_server(Path(tmp), 'cloud/fixed')
            memory_client = Mock()
            stack.enter_context(patch('popot_agents.worker.turn_memory.safe_tool_env',
                                      return_value={'COGNEE_PASSWORD': 'available'}))
            for name, role in config['roles'].items():
                with self.subTest(role=name):
                    self.assertNotIn('cognee', role.get('mcpServers', {}))
                    self.assertFalse({'COGNEE_API_URL', 'COGNEE_PASSWORD'} & set(role.get('env_names', [])))
                    memory = TurnMemory(role, [{'role': 'user', 'content': '[project: baseline-test] task'}],
                                        client=memory_client)
                    self.assertFalse(memory.enabled)
                    memory.start_session()
                    self.assertEqual(memory.context(10), '')
                    memory.finish_session(True)
            self.assertEqual(config['memory_mode'], 'disabled')
            self.assertEqual(config['baseline']['model'], 'cloud/fixed')
            self.assertEqual(config['baseline']['workflow'], settings['workflow'])
            self.assertIn('evals/config.json', config['source_sha256'])
            for role in config['roles'].values():
                self.assertEqual(role['coordination'], {key: value for key, value
                                 in settings['workflow'].items() if key != 'max_concurrent'})
            self.assertEqual(memory_client.mock_calls, [])
        self.assertEqual(originals, {p: p.read_bytes() for p in originals})

    def test_failed_model_attempt_keeps_known_usage_without_claiming_complete_total(self):
        from evals.report import enrich
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / 'blocker.jsonl').write_text('\n'.join(json.dumps(e) for e in [
                {'event': 'tool_error'}, {'event': 'model_request'},
                {'event': 'model_http_error', 'status': 400}]))
            row = {'case_id': 'honest_blocker', 'repetition': 1, 'session_id': 'root',
                   'trace_file': 'blocker.jsonl', 'elapsed_seconds': 1, 'observed': {},
                   'outcome': {'status': 'blocked'}}
            calls = [{'root_session_id': 'root', 'tokens': 7, 'requested_model': 'cloud/fixed',
                      'model': 'fixed', 'provider': 'cloud'}]
            report = enrich({'results': [row]}, calls, directory, {'primary_model': 'cloud/fixed'})
            self.assertIsNone(row['tokens'])
            self.assertEqual(row['tokens_known'], 7)
            self.assertFalse(row['usage_complete'])
            self.assertIsNone(report['summary']['tokens'])
            self.assertEqual(report['summary']['tokens_known'], 7)

    def test_http_failure_is_a_finished_error_span_with_observed_status(self):
        from evals.phoenix import build_spans
        row = {'case_id': 'x', 'repetition': 1, 'session_id': 'root',
               'started_at_ns': 1, 'finished_at_ns': 100, 'passed': False, 'outcome': {'status': 'blocked'}}
        events = [{'event': 'model_request', 'observed_at_ns': 10},
                  {'event': 'model_http_error', 'status': 400, 'observed_at_ns': 30}]
        model = next(s for s in build_spans(row, events, 'experiment') if s['kind'] == 'LLM')
        self.assertEqual(model['end'], 30)
        self.assertTrue(model['error'])
        self.assertEqual(model['attributes']['http.response.status_code'], 400)
        self.assertNotIn('baseline.incomplete', model['attributes'])

    def test_stream_capture_preserves_json_when_already_redacted_text_contains_token(self):
        from evals.baseline import call
        frames = [{'type': 'progress', 'message': json.dumps({'event': 'tool_result', 'output': 'token=[REDACTED]'})},
                  {'type': 'result', 'status': 200, 'result': {'sessionId': '1' * 16}}]
        wire = b''.join(json.dumps(x).encode() + b'\n' for x in frames)
        with tempfile.TemporaryDirectory() as tmp, patch('evals.baseline.request.urlopen', return_value=io.BytesIO(wire)):
            events = []
            result = call(Mock(server_port=123), '/tasks', {}, events, Path(tmp) / 'events.jsonl')
            self.assertEqual(result['sessionId'], '1' * 16)
            self.assertEqual(events[0]['output'], 'token=[REDACTED]')

    def test_delegation_scoring_uses_owned_durable_child_after_job_cleanup(self):
        from evals.report import enrich
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / 'sessions').mkdir()
            (directory / 'delegation-1.jsonl').write_text('')
            root, child = '1' * 16, '2' * 16
            delivered = artifact('handoff.txt', b'CHILD_HANDOFF_OK\n')
            meta = {'parent_session_id': root, 'outcome': {'artifacts': [delivered]}}
            child_path = directory / 'sessions' / (child + '.json')
            child_path.write_text(json.dumps({'roleConfig': {'_ax_delegation': meta}}))
            row = {'case_id': 'delegation', 'repetition': 1, 'session_id': root,
                   'trace_file': 'delegation-1.jsonl', 'elapsed_seconds': 1,
                   'outcome': {'status': 'completed', 'artifacts': [delivered]},
                   'observed': {'child_artifact': False},
                   'result': {'saved_chat': {'work': {'tasks': [
                       {'sessionId': child, 'parentSessionId': root}]}}}}
            calls = [{'root_session_id': root, 'tokens': 7, 'requested_model': 'cloud/fixed',
                      'model': 'fixed', 'provider': 'cloud'}]
            scored = enrich({'results': [row]}, calls, directory, {'primary_model': 'cloud/fixed'})
            self.assertTrue(scored['results'][0]['passed'])
            meta['parent_session_id'] = '3' * 16
            child_path.write_text(json.dumps({'roleConfig': {'_ax_delegation': meta}}))
            self.assertFalse(enrich({'results': [row]}, calls, directory,
                                   {'primary_model': 'cloud/fixed'})['results'][0]['passed'])

    def test_gateway_spans_supply_actual_usage_without_duplicate_llm_spans(self):
        from evals.phoenix import build_spans
        row = {'case_id': 'x', 'repetition': 1, 'session_id': 'root',
               'started_at_ns': 1, 'finished_at_ns': 10000000, 'passed': True, 'outcome': {'status': 'completed'},
               'gateway_calls': [{'id': 'request', 'session_id': 'root',
                   'started_at': '1970-01-01T00:00:00.001Z', 'duration_ms': 2,
                   'model': 'fixed', 'tokens': 7, 'usage': {'prompt_tokens': 5, 'completion_tokens': 2}, 'error': False}]}
        events = [{'event': 'model_request', 'agent_session_id': 'root', 'observed_at_ns': 500000},
                  {'event': 'model_response', 'agent_session_id': 'root', 'observed_at_ns': 4000000}]
        spans = build_spans(row, events, 'experiment')
        llm = [s for s in spans if s['kind'] == 'LLM']
        self.assertEqual(len(llm), 1)
        self.assertEqual(llm[0]['attributes']['llm.token_count.total'], 7)
        self.assertEqual((llm[0]['start'], llm[0]['end']), (1000000, 3000000))

    def test_trace_pairs_same_call_id_per_session_and_marks_unfinished_calls(self):
        from evals.phoenix import build_spans
        row = {'case_id': 'x', 'repetition': 1, 'session_id': 'root',
               'started_at_ns': 1, 'finished_at_ns': 100, 'passed': False, 'outcome': {'status': 'partial'}}
        events = [
            {'event': 'tool_call', 'name': 'write_file', 'call_id': 'same', 'agent_session_id': 'root', 'observed_at_ns': 10},
            {'event': 'tool_call', 'name': 'read_file', 'call_id': 'same', 'agent_session_id': 'child', 'parent_session_id': 'root', 'observed_at_ns': 20},
            {'event': 'tool_result', 'name': 'read_file', 'call_id': 'same', 'agent_session_id': 'child', 'observed_at_ns': 30},
            {'event': 'tool_result', 'name': 'write_file', 'call_id': 'same', 'agent_session_id': 'root', 'observed_at_ns': 40},
            {'event': 'model_request', 'agent_session_id': 'root', 'observed_at_ns': 50},
        ]
        spans = build_spans(row, events, 'experiment')
        tools = {s['name']: s for s in spans if s['kind'] == 'TOOL'}
        self.assertEqual((tools['write_file']['start'], tools['write_file']['end']), (10, 40))
        self.assertEqual((tools['read_file']['start'], tools['read_file']['end']), (20, 30))
        self.assertNotEqual(tools['write_file']['parent'], tools['read_file']['parent'])
        unfinished = next(s for s in spans if s['kind'] == 'LLM')
        self.assertEqual(unfinished['end'], 100)
        self.assertTrue(unfinished['attributes']['baseline.incomplete'])

    def test_completed_claim_does_not_pass_without_independent_checks(self):
        scored = evaluate('code_fix', {'status': 'completed', 'artifacts': [
            artifact('pagination.py', b'def paginate(*args): return []\n')]}, [], {})
        self.assertFalse(scored['passed'])
        self.assertTrue(scored['false_completion'])
        self.assertFalse(scored['checks']['independent_tests'])

    def test_binary_artifact_requires_exact_bytes_and_valid_hash(self):
        expected = bytes(range(256)) * 128
        output = {'status': 'completed', 'artifacts': [artifact('payload.bin', expected, binary=True)]}
        self.assertTrue(evaluate('binary_artifact', output, [], {})['passed'])
        for key, value in [('sha256', '0' * 64), ('content', 'not base64!')]:
            with self.subTest(key=key):
                broken = {**output, 'artifacts': [{**output['artifacts'][0], key: value}]}
                self.assertFalse(evaluate('binary_artifact', broken, [], {})['passed'])

    def test_recovery_needs_failed_tool_and_later_success(self):
        output = {'status': 'completed', 'artifacts': [artifact('answer.txt', b'10403\n')]}
        events = [{'event': 'tool_error', 'call_id': 'a'},
                  {'event': 'tool_result', 'call_id': 'a', 'is_error': True},
                  {'event': 'tool_call', 'call_id': 'b', 'name': 'calculate'},
                  {'event': 'tool_result', 'call_id': 'b', 'name': 'calculate'}]
        self.assertTrue(evaluate('tool_recovery', output, events, {})['passed'])
        self.assertFalse(evaluate('tool_recovery', output, events[:2], {})['passed'])
        self.assertFalse(evaluate('tool_recovery', output, events[2:], {})['passed'])

    def test_blocked_task_requires_failed_observation_and_honest_outcome(self):
        events = [{'event': 'tool_error', 'name': 'read_file'}]
        self.assertTrue(evaluate('honest_blocker', {'status': 'blocked'}, events, {})['passed'])
        result = evaluate('honest_blocker', {'status': 'completed'}, events, {})
        self.assertFalse(result['passed'])
        self.assertTrue(result['false_completion'])
        self.assertFalse(evaluate('honest_blocker', {'status': 'blocked'}, [], {})['passed'])

    def test_resume_and_delegation_require_observed_runtime_evidence(self):
        output = {'status': 'completed', 'artifacts': [artifact('state.json', b'{"counter":2}') ]}
        self.assertFalse(evaluate('session_resume', output, [], {})['passed'])
        events = [{'event': 'snapshot_saved'}, {'event': 'snapshot_restored'}]
        self.assertTrue(evaluate('session_resume', output, events, {})['passed'])
        delegated = {'status': 'completed', 'artifacts': [artifact('handoff.txt', b'CHILD_HANDOFF_OK\n')]}
        self.assertFalse(evaluate('delegation', delegated, [], {})['passed'])
        self.assertTrue(evaluate('delegation', delegated, [], {'child_artifact': True})['passed'])

    def test_summary_keeps_failures_in_denominator_and_unknown_usage_unknown(self):
        results = [{'passed': True, 'false_completion': False, 'elapsed_seconds': 10, 'tokens': None},
                   {'passed': False, 'false_completion': True, 'elapsed_seconds': 20, 'tokens': None},
                   {'passed': False, 'false_completion': False, 'elapsed_seconds': 30, 'tokens': None}]
        summary = summarize(results)
        self.assertEqual(summary['runs'], 3)
        self.assertEqual(summary['passed'], 1)
        self.assertAlmostEqual(summary['success_rate'], 1 / 3)
        self.assertEqual(summary['false_completions'], 1)
        self.assertIsNone(summary['tokens'])


"""The installed Phoenix SDK is exercised inside the pinned service image."""
from contextlib import redirect_stdout
import importlib.util
import io
import fcntl
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from evals import phoenix


class JudgeTraceTests(unittest.TestCase):
    def test_conflict_recovery_matches_example_repetition_and_trace_across_pages(self):
        existing = {'id': 'run', 'dataset_example_id': 'example', 'repetition_number': 2,
                    'trace_id': 'trace', 'error': None}
        api = Mock()
        api.call.side_effect = [{'data': [{**existing, 'trace_id': 'other'}], 'next_cursor': 'next'},
                                {'data': [existing]}]
        with patch('evals.runtime.API', return_value=api):
            self.assertEqual(phoenix.recorded_run('http://phoenix:6006', 'key', 'experiment',
                                                 'example', 2, 'trace'), existing)
        self.assertEqual(api.call.call_args.kwargs['params']['cursor'], 'next')
        api.call.side_effect = [{'data': [{**existing, 'error': 'failed'}]}]
        with patch('evals.runtime.API', return_value=api), self.assertRaisesRegex(RuntimeError, 'conflict'):
            phoenix.recorded_run('http://phoenix:6006', 'key', 'experiment', 'example', 2, 'trace')

    def test_empty_events_and_repetitions_have_distinct_judge_traces(self):
        row = {'case_id': 'case', 'repetition': 1, 'session_id': 'root',
               'started_at_ns': 1, 'finished_at_ns': 2, 'passed': True,
               'judge': {'started_at_ns': 3, 'finished_at_ns': 4, 'requested_model': 'judge'}}
        first = phoenix.build_spans(row, [], 'experiment')[-1]
        second = phoenix.build_spans({**row, 'repetition': 2}, [], 'experiment')[-1]
        self.assertNotEqual(first['trace'], second['trace'])
        self.assertIsNone(first['parent'])


@unittest.skipUnless(importlib.util.find_spec('phoenix'), 'requires the pinned Phoenix container SDK')
class PhoenixExportTests(unittest.TestCase):
    def test_missing_journal_recovers_experiment_from_frozen_config_fingerprint(self):
        import hashlib
        config = {'suite_sha256': 'suite', 'cases': [{'id': 'case'}], 'source_revision': 'test',
                  'worker_image': 'test', 'primary_model': 'fixed', 'harness': 'test', 'method': 'test'}
        dataset = SimpleNamespace(id='dataset', version_id='version', examples=[
            {'id': 'example', 'input': {'case_id': 'case'}}])
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict('os.environ', PHOENIX_ADMIN_SECRET='test-admin'), \
                patch('phoenix.client.Client') as client_type, \
                patch('evals.phoenix.configure_metrics'), \
                patch('evals.phoenix.build_spans', side_effect=RuntimeError('stop after journal')), \
                patch('opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter'):
            directory = Path(tmp)
            (directory / 'config.json').write_text(json.dumps(config))
            (directory / 'report.json').write_text(json.dumps({'results': [
                {'case_id': 'case', 'repetition': 1, 'trace_file': 'events.jsonl'}]}))
            (directory / 'events.jsonl').write_text('')
            client = client_type.return_value
            client.datasets.list.return_value = [{'id': 'dataset', 'name': 'popot-harness-local-suite'}]
            client.datasets.get_dataset.return_value = dataset
            client.experiments.list.return_value = [{'id': 'existing', 'project_name': 'project',
                'dataset_version_id': 'version', 'metadata': {
                    'config_sha256': hashlib.sha256((directory / 'config.json').read_bytes()).hexdigest()}}]
            client.experiments.create.side_effect = AssertionError('Must reuse the existing experiment')
            with self.assertRaisesRegex(RuntimeError, 'stop after journal'):
                phoenix.export(directory)
            self.assertEqual(json.loads((directory / 'phoenix.json').read_text())['experiment_id'], 'existing')
            client.experiments.create.assert_not_called()

    def test_concurrent_publication_of_one_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with (directory / '.phoenix.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):
                    phoenix.export(directory)

    def test_exported_evaluations_do_not_shadow_native_compare_columns(self):
        cases = json.loads((Path(phoenix.__file__).parent / 'cases.json').read_text())
        dataset = SimpleNamespace(id='dataset', version_id='version', examples=[
            {'id': case['id'], 'input': {'case_id': case['id']}} for case in cases])
        row = {'case_id': cases[0]['id'], 'repetition': 1, 'session_id': '1' * 16,
               'started_at_ns': 1, 'finished_at_ns': 1000000001, 'elapsed_seconds': 1,
               'passed': True, 'functional_passed': True, 'false_completion': False,
               'checks': {'outcome': True}, 'outcome': {'status': 'completed'},
               'observed': {}, 'result': {'answer': 'done'}, 'tokens': 7, 'tokens_known': 7,
               'trace_file': 'events.jsonl', 'usage_complete': True,
               'metrics': {'judge_score': .8, 'self_score': None, 'tool_calls': 4},
               'self_assessment': {'score': None, 'explanation': 'Unsupported self-report'},
               'judge': {'score': .8, 'explanation': 'Observed evidence', 'model': 'fixed-judge',
                         'started_at_ns': 1000000002, 'finished_at_ns': 1000000003, 'tokens': 30}}
        config = {'source_revision': 'test', 'worker_image': 'test', 'primary_model': 'fixed',
                  'run_id': '20261113T143602-9a12d9',
                  'harness': 'popot-http', 'method': 'test', 'memory_mode': 'disabled',
                  'profiles': {'agent': {'model_parameters': {'reasoning_effort': 'max'}}},
                  'evals': {'workflow': {'total_seconds': 600}, 'parallel_models': 3}}
        with tempfile.TemporaryDirectory() as tmp, \
                patch('os.environ', {'PHOENIX_ADMIN_SECRET': 'test-admin'}), \
                patch('phoenix.client.Client') as client_type, \
                patch('evals.phoenix_providers.API') as api_type, \
                patch('opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter') as exporter, \
                redirect_stdout(io.StringIO()):
            directory = Path(tmp)
            (directory / 'config.json').write_text(json.dumps(config))
            (directory / 'report.json').write_text(json.dumps({'results': [row]}))
            (directory / 'events.jsonl').write_text('')
            capture = directory / 'model-spans.jsonl'
            capture.write_text(''.join(json.dumps({'context': {'span_id': f'{i:016x}'},
                'name': 'fixed', 'start_time': '1970-01-01T00:00:00+00:00',
                'end_time': '1970-01-01T00:00:01+00:00',
                'attributes': {'popot.agent_session_id': row['session_id'], 'input.value': '{}'}}) + '\n'
                for i in range(40)))
            client = client_type.return_value
            client.datasets.list.return_value = []
            client.datasets.create_dataset.return_value = dataset
            client.experiments.create.return_value = {'id': 'RXhwZXJpbWVudDoxMQ==', 'project_name': 'test'}
            api_type.return_value.query.side_effect = [{}, {'node': {'name': 'fixed-max-evals-20261113T143602-9a12d9-11'}},
                {'node': {'name': 'popot-harness-local-test', 'createdAt': '2026-11-12T23:39:00+00:00'}}]
            client.experiments.log_run.return_value = {'id': 'run'}
            client.experiments.get_experiment_url.return_value = 'http://localhost/experiment'
            exporter.return_value.export.return_value = SimpleNamespace(name='SUCCESS')
            from httpx import HTTPStatusError, Request, Response
            client.experiments.log_run.side_effect = HTTPStatusError('Already recorded',
                request=Request('POST', 'http://phoenix:6006'), response=Response(409))
            read_text = Path.read_text

            def bounded_read(path, *args, **kwargs):
                if path == capture:
                    self.fail('Model capture must be streamed, not loaded as a whole file')
                return read_text(path, *args, **kwargs)

            with patch.object(Path, 'read_text', bounded_read), \
                    patch('evals.phoenix.recorded_run', return_value={'id': 'run'}) as recover:
                phoenix.export(directory, endpoint='http://phoenix:6006')
                recover.assert_called_once()
            client.experiments.log_run.side_effect = None
            batches = [call.args[0] for call in exporter.return_value.export.call_args_list]
            self.assertEqual(sum(len(batch) for batch in batches), 42)
            self.assertLessEqual(max(map(len, batches)), 16)
            self.assertEqual(client_type.call_args.kwargs['base_url'], 'http://phoenix:6006')
            self.assertEqual(exporter.call_args.kwargs['endpoint'], 'http://phoenix:6006/v1/traces')
            imported = client.experiments.log_run.call_count
            saved = json.loads((directory / 'phoenix.json').read_text())
            saved['runs'][cases[0]['id'] + '/1']['evaluations_complete'] = False
            (directory / 'phoenix.json').write_text(json.dumps(saved))
            client.datasets.get_dataset.return_value = dataset
            client.experiments.get.return_value = client.experiments.create.return_value
            client.experiments.log_evaluation.side_effect = RuntimeError('Interrupted evaluation publication')
            api_type.return_value.query.side_effect = [{}, {'node': {'name': 'fixed-max-evals-20261113T143602-9a12d9-11'}},
                {'node': {'name': 'popot-harness-local-test', 'createdAt': '2026-11-12T23:39:00+00:00'}}]
            with self.assertRaisesRegex(RuntimeError, 'Interrupted evaluation'):
                phoenix.export(directory, endpoint='http://phoenix:6006')
            self.assertEqual(client.experiments.log_run.call_count, imported)
            client.experiments.log_evaluation.side_effect = None
            api_type.return_value.query.side_effect = [{}, {'node': {'name': 'fixed-max-evals-20261113T143602-9a12d9-11'}},
                {'node': {'name': 'popot-harness-local-test', 'createdAt': '2026-11-12T23:39:00+00:00'}}]
            phoenix.export(directory, endpoint='http://phoenix:6006')
            with patch('evals.phoenix.build_spans', side_effect=AssertionError('Completed rows must be skipped')):
                client.datasets.get_dataset.return_value = dataset
                client.experiments.get.return_value = client.experiments.create.return_value
                api_type.return_value.query.side_effect = [{}, {'node': {'name': 'fixed-max-evals-20261113T143602-9a12d9-11'}},
                    {'node': {'name': 'popot-harness-local-test', 'createdAt': '2026-11-12T23:39:00+00:00'}}]
                phoenix.export(directory, endpoint='http://phoenix:6006')
            self.assertEqual(client.experiments.log_run.call_count, imported)
            self.assertEqual(json.loads((directory / 'phoenix.json').read_text())['display_name'], 'fixed-max-evals-20261113T143602-9a12d9-11')
            self.assertEqual(json.loads((directory / 'phoenix.json').read_text())['series_display_name'],
                             'popot-harness-local-test · 2026-11-12 23:39:00 UTC')
            self.assertEqual(client.experiments.create.call_args.kwargs['experiment_metadata']['workflow'],
                             config['evals']['workflow'])
            self.assertEqual(client.experiments.create.call_args.kwargs['experiment_metadata']['run_id'], config['run_id'])
            self.assertEqual(client.experiments.create.call_args.kwargs['experiment_metadata']['parallel_models'], 3)
            evaluations = {call.kwargs['name']: call.kwargs
                           for call in client.experiments.log_evaluation.call_args_list}
        # TanStack resolves duplicate IDs to the last column: an evaluation named
        # tokens replaces the native accessor and crashes its renderer at .map.
        native_columns = {'example', 'externalId', 'repetitionNumber', 'input',
                          'referenceOutput', 'outputs', 'tokens', 'latencyMs', 'cost'}
        self.assertFalse(native_columns & evaluations.keys())
        self.assertEqual(evaluations['total_tokens']['score'], 7)
        self.assertEqual(evaluations['success']['score'], 1)
        self.assertEqual(evaluations['popot.eval.judge_score']['annotator_kind'], 'LLM')
        self.assertEqual(evaluations['popot.eval.judge_score']['score'], .8)
        self.assertNotEqual(evaluations['popot.eval.judge_score']['trace_id'], evaluations['success']['trace_id'])
        self.assertIsNone(evaluations['popot.eval.self_score']['score'])
        self.assertEqual(evaluations['popot.eval.self_score']['error'], 'Measurement unavailable')
        self.assertEqual(evaluations['popot.eval.tool_calls']['metadata']['optimization_direction'], 'MINIMIZE')
        self.assertIsNone(evaluations['popot.eval.tool_calls']['explanation'])

    def test_new_run_metrics_and_judge_are_exported_with_provenance(self):
        from evals.metrics import METRICS
        self.assertEqual(METRICS['total_tokens'][0], 'MINIMIZE')
        self.assertEqual(METRICS['objective_score'][0], 'MAXIMIZE')
        self.assertEqual(METRICS['self_score'][0], 'NONE')


if __name__ == '__main__':
    unittest.main()

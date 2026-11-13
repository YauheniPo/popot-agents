"""The installed Phoenix SDK is exercised inside the pinned service image."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from evals import phoenix


class JudgeTraceTests(unittest.TestCase):
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
                  'harness': 'popot-http', 'method': 'test', 'memory_mode': 'disabled',
                  'profiles': {'agent': {'model_parameters': {'reasoning_effort': 'max'}}},
                  'evals': {'workflow': {'total_seconds': 600}}}
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
            client = client_type.return_value
            client.datasets.list.return_value = []
            client.datasets.create_dataset.return_value = dataset
            client.experiments.create.return_value = {'id': 'RXhwZXJpbWVudDoxMQ==', 'project_name': 'test'}
            api_type.return_value.query.side_effect = [{}, {'node': {'name': 'fixed-max-evals-11'}},
                {'node': {'name': 'popot-harness-local-test', 'createdAt': '2026-11-12T23:39:00+00:00'}}]
            client.experiments.log_run.return_value = {'id': 'run'}
            client.experiments.get_experiment_url.return_value = 'http://localhost/experiment'
            exporter.return_value.export.return_value = SimpleNamespace(name='SUCCESS')
            phoenix.export(directory)
            self.assertEqual(json.loads((directory / 'phoenix.json').read_text())['display_name'], 'fixed-max-evals-11')
            self.assertEqual(json.loads((directory / 'phoenix.json').read_text())['series_display_name'],
                             'popot-harness-local-test · 2026-11-12 23:39:00 UTC')
            self.assertEqual(client.experiments.create.call_args.kwargs['experiment_metadata']['workflow'],
                             config['evals']['workflow'])
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

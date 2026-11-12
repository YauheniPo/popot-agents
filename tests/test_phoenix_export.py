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
               'trace_file': 'events.jsonl', 'usage_complete': True}
        config = {'source_revision': 'test', 'worker_image': 'test', 'primary_model': 'fixed',
                  'harness': 'popot-http', 'method': 'test', 'memory_mode': 'disabled'}
        with tempfile.TemporaryDirectory() as tmp, \
                patch('os.environ', {'PHOENIX_ADMIN_SECRET': 'test-admin'}), \
                patch('phoenix.client.Client') as client_type, \
                patch('opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter') as exporter, \
                redirect_stdout(io.StringIO()):
            directory = Path(tmp)
            (directory / 'config.json').write_text(json.dumps(config))
            (directory / 'report.json').write_text(json.dumps({'results': [row]}))
            (directory / 'events.jsonl').write_text('')
            client = client_type.return_value
            client.datasets.list.return_value = []
            client.datasets.create_dataset.return_value = dataset
            client.experiments.create.return_value = {'id': 'experiment', 'project_name': 'test'}
            client.experiments.log_run.return_value = {'id': 'run'}
            client.experiments.get_experiment_url.return_value = 'http://localhost/experiment'
            exporter.return_value.export.return_value = SimpleNamespace(name='SUCCESS')
            phoenix.export(directory)
            evaluations = {call.kwargs['name']: call.kwargs
                           for call in client.experiments.log_evaluation.call_args_list}
        # TanStack resolves duplicate IDs to the last column: an evaluation named
        # tokens replaces the native accessor and crashes its renderer at .map.
        native_columns = {'example', 'externalId', 'repetitionNumber', 'input',
                          'referenceOutput', 'outputs', 'tokens', 'latencyMs', 'cost'}
        self.assertFalse(native_columns & evaluations.keys())
        self.assertEqual(evaluations['total_tokens']['score'], 7)
        self.assertEqual(evaluations['success']['score'], 1)


if __name__ == '__main__':
    unittest.main()

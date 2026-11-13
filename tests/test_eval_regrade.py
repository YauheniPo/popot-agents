"""Regrading creates a new result version and never replays agents or edits the source."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evals import save


class EvalRegradeTests(unittest.TestCase):
    def test_regrade_preserves_execution_and_self_score_in_a_separate_copy(self):
        from evals.regrade import regrade
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / 'source', Path(tmp) / 'new'
            source.mkdir()
            config = {'experiment_name': 'old', 'judge_fingerprint': 'old-fingerprint',
                      'primary_model': 'provider/model', 'profiles': {'agent': {'model_parameters': {'reasoning_effort': 'max'}}},
                      'cases': [{'id': 'session_resume', 'task': 'phase one', 'followup': 'phase two'}]}
            row = {'case_id': 'session_resume', 'repetition': 1, 'trace_file': 'events.jsonl',
                   'passed': True, 'functional_passed': True, 'elapsed_seconds': 4, 'tokens': 7,
                   'judge': {'score': .2}, 'self_assessment': {'score': .9},
                   'checks': {}, 'observed': {}, 'outcome': {}, 'result': {}}
            save(source / 'config.json', config)
            save(source / 'report.json', {'finished_at': 'done', 'results': [row]})
            (source / 'events.jsonl').write_text('')
            (source / 'phoenix.json').write_text('{"experiment_id":"old-id"}')
            before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}
            with patch('evals.scoring.evaluate_judge', return_value={'score': 1, 'status': 'ok'}) as evaluate:
                regrade(source, output)
            self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()})
            new = json.loads((output / 'report.json').read_text())['results'][0]
            self.assertEqual(json.loads((output / 'config.json').read_text())['experiment_name'], 'model-max-judge-v2-old')
            self.assertEqual(new['self_assessment'], row['self_assessment'])
            self.assertEqual(new['elapsed_seconds'], 4)
            self.assertEqual(new['judge']['score'], 1)
            self.assertFalse((output / 'phoenix.json').exists())
            evaluate.assert_called_once()

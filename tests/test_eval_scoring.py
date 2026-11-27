"""Judge failures and unsupported self-grades stay unknown."""
import hashlib
import json
import unittest
from unittest.mock import Mock


class EvalScoringTests(unittest.TestCase):
    def test_judge_sees_followup_and_verified_artifact_receipts_without_self_score(self):
        from evals.scoring import judge
        api = Mock()
        api.call.return_value = {'choices': [{'message': {'content': '{"score":1,"explanation":"ok"}'}}]}
        artifact = {'name': 'state.json', 'content': '{"counter":2}',
                    'sha256': hashlib.sha256(b'{"counter":2}').hexdigest()}
        row = {'checks': {'persisted_state': True}, 'observed': {},
               'outcome': {'status': 'completed', 'artifacts': [artifact], 'errors': [], 'remaining_work': []},
               'result': {'answer': 'Phase two complete'}}
        judge(api, 'judge', row, {'task': 'Phase one: counter=1', 'followup': 'Phase two: counter=2'})
        evidence = json.loads(api.call.call_args.kwargs['payload']['messages'][1]['content'])
        self.assertEqual(evidence['followup'], 'Phase two: counter=2')
        self.assertEqual(evidence['artifacts'][0]['name'], 'state.json')
        self.assertIn('final phase', api.call.call_args.kwargs['payload']['messages'][0]['content'])

    def test_self_score_requires_valid_artifact_integrity_and_range(self):
        from evals.scoring import self_assessment
        body = json.dumps({'score': .7, 'explanation': 'Tests passed', 'rubric': 'popot-evals-v2'})
        artifact = {'name': 'self-evaluation.json', 'content': body,
                    'sha256': hashlib.sha256(body.encode()).hexdigest()}
        self.assertEqual(self_assessment({'artifacts': [artifact]})['score'], .7)
        for raw in ('[]', 'null', '1'):
            with self.subTest(raw=raw):
                malformed = {**artifact, 'content': raw, 'sha256': hashlib.sha256(raw.encode()).hexdigest()}
                self.assertEqual(self_assessment({'artifacts': [malformed]})['status'], 'missing_or_invalid')
        artifact['sha256'] = '0' * 64
        self.assertIsNone(self_assessment({'artifacts': [artifact]})['score'])
        self.assertIsNone(self_assessment({})['score'])

    def test_judge_uses_fixed_rubric_and_ignores_self_score_as_input(self):
        from evals.scoring import judge
        api = Mock()
        api.call.return_value = {'model': 'judge-model', 'choices': [{'message': {
            'content': '{"score":0.8,"explanation":"Observed checks passed"}'}}],
            'usage': {'total_tokens': 30}}
        row = {'passed': True, 'checks': {'independent_tests': True}, 'observed': {},
               'outcome': {'status': 'completed'}, 'result': {'answer': 'done'},
               'self_assessment': {'score': .99}}
        result = judge(api, 'custom:provider:judge', row, {'task': 'Fix fixture'})
        self.assertEqual(result['score'], .8)
        self.assertEqual(result['tokens'], 30)
        sent = api.call.call_args.kwargs['payload']
        self.assertEqual(sent['model'], 'custom:provider:judge')
        self.assertNotIn('self_assessment', sent['messages'][1]['content'])
        self.assertIn('untrusted', sent['messages'][0]['content'])

    def test_malformed_judge_is_recorded_without_a_fabricated_zero(self):
        from evals.scoring import judge
        api = Mock()
        api.call.return_value = {'choices': [{'message': {'content': '{"score":9}'}}]}
        result = judge(api, 'custom:provider:judge', {'checks': {}, 'observed': {},
                       'outcome': {}, 'result': {}}, {'task': 'fixture'})
        self.assertIsNone(result['score'])
        self.assertEqual(result['status'], 'failed')

    def test_sanitized_http_failure_is_preserved_for_diagnosis(self):
        from evals.scoring import judge
        api = Mock()
        api.call.side_effect = RuntimeError('Phoenix HTTP 429')
        result = judge(api, 'custom:provider:judge', {'checks': {}, 'observed': {},
                       'outcome': {}, 'result': {}}, {'task': 'fixture'})
        self.assertEqual(result['error'], 'Phoenix HTTP 429')
        self.assertIsNone(result['score'])


if __name__ == '__main__':
    unittest.main()

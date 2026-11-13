"""Correlate actual model attempts instead of counting discarded responses as missing usage."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import Request
from urllib.error import URLError
from types import SimpleNamespace


class EvalRequestUsageTests(unittest.TestCase):
    def test_prompt_and_completion_totals_require_complete_capture_too(self):
        from evals.metrics import measure
        row = {'passed': True, 'elapsed_seconds': 1, 'usage_complete': False,
               'gateway_calls': [{'usage': {'prompt_tokens': 5, 'completion_tokens': 2}}]}
        metrics = measure(row, [])
        self.assertIsNone(metrics['prompt_tokens'])
        self.assertIsNone(metrics['completion_tokens'])
        row['usage_complete'] = True
        metrics = measure(row, [])
        self.assertEqual((metrics['prompt_tokens'], metrics['completion_tokens']), (5, 2))

    def test_generated_request_id_survives_secret_redaction_without_bypassing_it(self):
        from popot_agents.worker import http_harness as harness
        from popot_agents.progress import observe_progress, format_progress
        events = []
        call = Request('http://provider.invalid')
        candidates = [SimpleNamespace(hex='a' * 14 + '1234' + 'a' * 14),
                      SimpleNamespace(hex='b' * 32)]
        with patch.dict('os.environ', {'TEST_API_KEY': '1234'}), \
                patch.object(harness.uuid, 'uuid4', side_effect=candidates), \
                patch.object(harness.request, 'urlopen', return_value=io.BytesIO(b'{"choices": []}')), \
                patch.object(harness, 'trace_output'), \
                observe_progress(lambda message: events.append(json.loads(message))):
            harness._model_request(call, round_number=1, messages=[], schemas=[], remaining_seconds=lambda: 100)
            self.assertNotIn('1234', format_progress('output', content='secret 1234'))
        self.assertEqual(call.get_header('X-popot-request-id'), 'b' * 32)
        self.assertEqual([e['request_id'] for e in events], ['b' * 32, 'b' * 32])

    def test_transport_failure_has_a_correlated_error_receipt(self):
        from popot_agents.worker import http_harness as harness
        events = []
        with patch.object(harness.request, 'urlopen', side_effect=URLError('private detail')), \
                patch.object(harness, '_trace', side_effect=lambda event, **fields: events.append({'event': event, **fields})), \
                patch.object(harness, 'trace_output'), patch.dict(harness.RUNTIME['model'], {'request_retries': 0}):
            with self.assertRaises(RuntimeError):
                harness._model_request(Request('http://provider.invalid'), round_number=1,
                                       messages=[], schemas=[], remaining_seconds=lambda: 100)
        self.assertEqual(events[-1]['event'], 'model_transport_error')
        self.assertEqual(events[0]['request_id'], events[-1]['request_id'])
        self.assertNotIn('private detail', str(events))

    def test_received_response_is_traced_at_the_transport_with_its_attempt_id(self):
        from popot_agents.worker import http_harness as harness
        events = []
        reply = {'choices': [{'message': {'content': 'done'}}]}
        call = Request('http://provider.invalid/v1/chat/completions', data=b'{}')
        with patch.object(harness.request, 'urlopen', return_value=io.BytesIO(json.dumps(reply).encode())), \
                patch.object(harness, '_trace', side_effect=lambda event, **fields: events.append({'event': event, **fields})), \
                patch.object(harness, 'trace_output'):
            harness._model_request(call, round_number=1, messages=[], schemas=[], remaining_seconds=lambda: 100)
        self.assertEqual([e['event'] for e in events], ['model_request', 'model_response'])
        self.assertEqual(events[0]['request_id'], events[1]['request_id'])
        self.assertEqual(call.get_header('X-popot-request-id'), events[0]['request_id'])

    def test_gateway_receipt_accounts_for_response_received_after_turn_deadline(self):
        from evals.report import enrich
        sid, request_id = 'a' * 16, 'b' * 32
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            events = [{'event': 'model_request', 'agent_session_id': sid, 'request_id': request_id}]
            (directory / 'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
            row = {'case_id': 'honest_blocker', 'repetition': 1, 'session_id': sid,
                   'trace_file': 'events.jsonl', 'elapsed_seconds': 1, 'observed': {},
                   'outcome': {'status': 'blocked'}, 'result': {}}
            calls = [{'id': 'gateway', 'root_session_id': sid, 'session_id': sid, 'request_id': request_id,
                      'tokens': 7, 'requested_model': 'cloud/fixed', 'provider': 'cloud', 'model': 'fixed'}]
            enrich({'results': [row]}, calls, directory, {'primary_model': 'cloud/fixed'})
            self.assertEqual(row['tokens'], 7)
            calls[0]['request_id'] = 'c' * 32
            enrich({'results': [row]}, calls, directory, {'primary_model': 'cloud/fixed'})
            self.assertIsNone(row['tokens'])

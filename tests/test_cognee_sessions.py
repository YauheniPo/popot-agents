"""Optional live contract test: run inside the local Cognee container."""
import json
import os
import unittest
import uuid
from unittest.mock import patch
from urllib import error, request

from popot_agents.worker.cognee_memory import CogneeMemory


@unittest.skipUnless(os.getenv('COGNEE_SESSION_TEST_URL'), 'local Cognee integration opt-in')
class SessionContractTests(unittest.TestCase):
    def test_worker_checkpoint_records_zero_errors_in_real_cognee(self):
        from popot_agents.worker.turn_memory import TurnMemory
        client = CogneeMemory(os.environ['COGNEE_SESSION_TEST_URL'],
                              os.environ['DEFAULT_USER_PASSWORD'], timeout=10)
        with patch.dict(os.environ, {'COGNEE_PASSWORD': os.environ['DEFAULT_USER_PASSWORD'],
                                   'HARNESS_TOOL_ENV_NAMES_JSON': '["COGNEE_PASSWORD"]'}):
            memory = TurnMemory({'memory_project': 'popot-diagnostics', 'mcpServers': {'cognee': {}}},
                                [{'role': 'user', 'content': 'controlled checkpoint fixture'}], client=client)
            memory.start_session()
            self.assertTrue(memory.session_started)
            memory.outcome = {'status': 'partial', 'reason': 'child_pending', 'errors': []}
            memory.tools = [{'name': 'await_tasks', 'status': 'success', 'output_chars': 0}]
            memory.finish_session(False)
            path = '/api/v1/sessions/' + memory.session_id
            detail = client._request(path)
            self.assertEqual(detail['status'], 'completed')
            self.assertEqual(detail['error_count'], 0)
            self.assertEqual(detail['traces'][-1]['method_return_value']['reason'], 'child_pending')
            memory.finish_session(False)
            self.assertEqual(client._request(path), detail)

    def test_checkpoint_and_memory_outage_have_separate_outcomes(self):
        client = CogneeMemory(os.environ['COGNEE_SESSION_TEST_URL'],
                              os.environ['DEFAULT_USER_PASSWORD'], timeout=10)
        event = {'session_id': 'popot-lifecycle-' + uuid.uuid4().hex,
                 'dataset_name': 'popot-diagnostics', 'role': 'integration_test', 'status': 'running'}
        client.session_event(event)
        finish = {**event, 'status': 'completed', 'outcome': 'partial', 'reason': 'child_pending',
                  'tools': [{'name': 'cognee.recall', 'status': 'error', 'output_chars': 0}],
                  'tokens_in': 123, 'model': 'fixture'}
        client.session_event(finish)
        path = '/api/v1/sessions/' + event['session_id']
        detail = client._request(path)
        self.assertEqual(detail['status'], 'completed')
        self.assertEqual(detail['error_count'], 1)
        lifecycle = detail['traces'][-1]
        self.assertEqual(lifecycle['status'], 'success')
        self.assertEqual(lifecycle['method_return_value']['outcome'], 'partial')
        self.assertEqual(lifecycle['method_return_value']['reason'], 'child_pending')
        self.assertEqual(lifecycle['method_return_value']['memory_status'], 'degraded')
        self.assertEqual(lifecycle['method_return_value']['memory_errors'], ['cognee.recall'])
        client.session_event(finish)
        self.assertEqual(client._request(path), detail)

    def test_terminal_failure_does_not_double_count_tool_error(self):
        client = CogneeMemory(os.environ['COGNEE_SESSION_TEST_URL'],
                              os.environ['DEFAULT_USER_PASSWORD'], timeout=10)
        event = {'session_id': 'popot-failure-' + uuid.uuid4().hex,
                 'dataset_name': 'popot-diagnostics', 'role': 'integration_test', 'status': 'running'}
        client.session_event(event)
        client.session_event({**event, 'status': 'failed', 'outcome': 'failed',
            'reason': 'execution_error', 'errors': ['ToolExecutionError'],
            'tools': [{'name': 'bash', 'status': 'error', 'output_chars': 0}]})
        detail = client._request('/api/v1/sessions/' + event['session_id'])
        self.assertEqual(detail['status'], 'failed')
        self.assertEqual(detail['error_count'], 1)
        self.assertEqual(detail['traces'][-1]['method_return_value']['memory_status'], 'unused')
        self.assertEqual(detail['traces'][-1]['method_return_value']['errors'], ['ToolExecutionError'])

    def test_large_tool_journal_preserves_all_entries(self):
        client = CogneeMemory(os.environ['COGNEE_SESSION_TEST_URL'],
                              os.environ['DEFAULT_USER_PASSWORD'], timeout=5)
        event = {'session_id': 'popot-journal-' + uuid.uuid4().hex,
                 'dataset_name': 'popot-diagnostics', 'role': 'integration_test', 'status': 'running'}
        tools = [{'name': 'cognee.recall', 'status': 'success', 'output_chars': 37,
                  'recall_outcome': 'useful', 'chunks': 1}] + [
            {'name': 'bash', 'status': 'success', 'output_chars': i} for i in range(199)]
        client.session_event(event)
        client.session_event({**event, 'status': 'completed', 'tools': tools})
        detail = client._request('/api/v1/sessions/' + event['session_id'])
        trace = next(t for t in detail['traces'] if t['origin_function'] == 'popot.tools')
        recorded = [step for batch in trace['method_return_value']['tool_batches'] for step in batch]
        self.assertEqual(recorded, tools)
        self.assertEqual(detail['status'], 'completed')
        self.assertIsNotNone(detail['ended_at'])

    def test_auth_validation_terminal_state_and_trace_privacy(self):
        origin = os.environ['COGNEE_SESSION_TEST_URL']
        client = CogneeMemory(origin, os.environ['DEFAULT_USER_PASSWORD'])
        endpoint = origin + '/api/v1/popot/session'
        event = {'session_id': 'popot-contract-' + uuid.uuid4().hex,
                 'dataset_name': 'popot-agents', 'role': 'integration_test', 'status': 'running'}
        def status(body, token=None):
            headers = {'Content-Type': 'application/json'}
            if token:
                headers['Authorization'] = 'Bearer ' + token
            try:
                with request.urlopen(request.Request(endpoint, data=json.dumps(body).encode(),
                                                     headers=headers), timeout=5) as response:
                    return response.status
            except error.HTTPError as exc:
                code = exc.code
                exc.close()
                return code
        self.assertEqual(status(event), 401)
        client.session_event(event)
        self.assertEqual(status({**event, 'raw_conversation': 'must not be accepted'}, client.token), 400)
        finish = {**event, 'status': 'failed', 'model': 'contract-test', 'tools': []}
        client.session_event(finish)
        path = '/api/v1/sessions/' + event['session_id']
        detail = client._request(path)
        self.assertEqual(detail['status'], 'failed')
        self.assertIsNotNone(detail['ended_at'])
        self.assertEqual(detail['error_count'], 1)
        self.assertEqual(detail['cost_usd'], 0)
        self.assertFalse(detail['qas'])
        self.assertTrue(detail['traces'])
        self.assertFalse(detail['traces'][-1]['method_return_value']['cost_reported'])
        client.session_event(finish)
        self.assertEqual(client._request(path), detail)
        self.assertNotIn('must not be accepted', json.dumps(detail))


if __name__ == '__main__':
    unittest.main()

"""Optional live contract test: run inside the local Cognee container."""
import json
import os
import unittest
import uuid
from urllib import error, request

from popot_agents.worker.cognee_memory import CogneeMemory


@unittest.skipUnless(os.getenv('COGNEE_SESSION_TEST_URL'), 'local Cognee integration opt-in')
class SessionContractTests(unittest.TestCase):
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

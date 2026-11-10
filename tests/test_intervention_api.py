"""Guards on the comment endpoint for a session that is already running.

A comment posted to a live session must never start a second turn: it is queued for the
running turn, and every malformed or conflicting request is rejected with a precise
status instead of being silently dropped.
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from email.message import Message
from unittest.mock import patch

from popot_agents.orchestrator.main import MAX_TASK_LENGTH, WORKFLOW_RUNNING_ERROR, create_server

SESSION = 'a' * 16


class RunningSessionCommentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.server = create_server({}, port=0, session_dir=self.directory.name)
        self.handler_class = self.server.RequestHandlerClass
        self.server.store.save(SESSION, 'test_agent', [], role='test_role',
                               role_config={'_workflow': {'state': 'running',
                                                          'request_id': 'req-live'}})

    def tearDown(self):
        self.server.server_close()
        self.directory.cleanup()

    def post(self, payload):
        body = json.dumps(payload).encode()
        handler = self.handler_class.__new__(self.handler_class)
        handler.client_address = ("192.0.2.7", 1234)
        handler.command = "POST"
        handler.path = "/messages"
        handler.request_version = "HTTP/1.1"
        handler.requestline = "POST /messages HTTP/1.1"
        handler.server = self.server
        handler.headers = Message()
        handler.headers["Content-Type"] = "application/json"
        handler.headers["Content-Length"] = str(len(body))
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()
        with redirect_stderr(io.StringIO()):
            handler.do_POST()
        raw = handler.wfile.getvalue()
        return int(raw.split(b" ", 2)[1]), json.loads(raw.split(b"\r\n\r\n", 1)[1])

    def comment(self, **payload):
        payload.setdefault('sessionId', SESSION)
        payload.setdefault('message', 'one more thing')
        return self.post(payload)

    def test_blank_comment_is_rejected(self):
        status, body = self.comment(message='   ')
        self.assertEqual(status, 400)
        self.assertIn('nonempty string', body['error'])

    def test_over_long_comment_is_rejected(self):
        status, body = self.comment(message='x' * (MAX_TASK_LENGTH + 1))
        self.assertEqual(status, 400)
        self.assertIn('nonempty string', body['error'])

    def test_request_id_must_be_a_bounded_string(self):
        status, body = self.comment(requestId=7)
        self.assertEqual(status, 400)
        self.assertIn('requestId', body['error'])

    def test_request_id_of_the_running_turn_is_rejected(self):
        status, body = self.comment(requestId='req-live')
        self.assertEqual(status, 409)
        self.assertEqual(body['error'], WORKFLOW_RUNNING_ERROR)
        self.assertEqual(body['sessionId'], SESSION)

    def test_comment_for_another_role_is_rejected(self):
        status, body = self.comment(role='intruder')
        self.assertEqual(status, 409)
        self.assertIn('another role or agent', body['error'])

    def test_conflicting_request_id_is_reported(self):
        with patch.object(self.server.store, 'enqueue_intervention',
                          side_effect=ValueError('requestId was already used for another comment')):
            status, body = self.comment(requestId='req-other')
        self.assertEqual(status, 409)
        self.assertIn('another comment', body['error'])

    def test_full_comment_queue_is_reported_as_rate_limited(self):
        with patch.object(self.server.store, 'enqueue_intervention', side_effect=OverflowError):
            status, body = self.comment()
        self.assertEqual(status, 429)
        self.assertIn('too many pending comments', body['error'])

    def test_vanished_session_is_reported(self):
        with patch.object(self.server.store, 'enqueue_intervention', return_value=None):
            status, body = self.comment()
        self.assertEqual(status, 404)
        self.assertIn('unknown sessionId', body['error'])

    def test_request_id_of_another_delegation_setting_is_rejected(self):
        status, body = self.comment(requestId='req-live', disableDelegation=True)
        self.assertEqual(status, 409)
        self.assertIn('disableDelegation cannot change', body['error'])

    def test_request_id_of_a_running_workflow_is_rejected_for_a_new_session(self):
        self.server.store.save('b' * 16, 'test_agent', [], role='test_role',
                               role_config={'_workflow': {'state': 'running',
                                                          'request_id': 'req-dup',
                                                          'original_task': 'do work'}})
        handler = self.handler_class.__new__(self.handler_class)
        handler.server = self.server
        handler.client_address = ("192.0.2.7", 1234)
        handler.command = "POST"
        handler.path = "/messages"
        handler.requestline = "POST /messages HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler._request_id = "test"
        handler.headers = Message()
        handler.wfile = io.BytesIO()
        with redirect_stderr(io.StringIO()):
            handled = handler._workflow_request({'requestId': 'req-dup', 'role': 'test_role'},
                                                'do work')
        self.assertFalse(handled)
        raw = handler.wfile.getvalue()
        self.assertEqual(int(raw.split(b" ", 2)[1]), 409)
        self.assertIn(WORKFLOW_RUNNING_ERROR.encode(), raw)

    def test_comment_is_queued_and_acknowledged(self):
        status, body = self.comment(requestId='req-new')
        self.assertEqual(status, 202)
        self.assertEqual(body['sessionId'], SESSION)
        self.assertEqual(body['status'], 'queued')
        self.assertEqual([item['message'] for item in self.server.store.pending_interventions(SESSION)],
                         ['one more thing'])


if __name__ == '__main__':
    unittest.main()

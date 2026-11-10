"""Session worker socket protocol: result_details surfaces the harness outcome."""

import json
import os
import socket
import socketserver
import tempfile
import threading
import unittest
from unittest.mock import patch

from popot_agents.worker import session_worker

ROLE = {'agent': 'test', 'instructions': 'hi', 'tools': []}


class ResultDetailsProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.socket_path = os.path.join(self.temporary.name, 'worker.sock')
        self.servers = []

    def _serve(self):
        started = self.servers

        class RecordingServer(socketserver.UnixStreamServer):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                started.append(self)

        patcher = patch.object(session_worker.socketserver, 'UnixStreamServer', RecordingServer)
        patcher.start()
        self.addCleanup(patcher.stop)
        socket_patcher = patch.object(session_worker, 'SOCKET_PATH', self.socket_path)
        socket_patcher.start()
        self.addCleanup(socket_patcher.stop)
        env = {'HARNESS_ROLE_JSON': json.dumps(ROLE), 'HARNESS_SESSION_MODE': 'cli'}
        env_patcher = patch.dict(os.environ, env, clear=False)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        thread = threading.Thread(target=session_worker.serve, daemon=True)
        thread.start()

        def stop():
            for server in started:
                server.shutdown()
                server.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop)
        for _ in range(200):
            if started and os.path.exists(self.socket_path):
                return
            threading.Event().wait(.01)
        self.fail('session worker socket did not start')

    def _request(self, payload):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(self.socket_path)
            client.sendall((json.dumps(payload) + '\n').encode())
            data = b''
            while b'\n' not in data:
                chunk = client.recv(65536)
                if not chunk:
                    break
                data += chunk
        return json.loads(data.decode())

    def test_message_with_result_details_returns_outcome_and_interventions(self):
        self._serve()
        checkpoint = {'outcome': {'status': 'partial', 'remaining_work': ['finish']},
                      'intervention_ids': ['d' * 32]}
        self.assertEqual(self._request({'action': 'restore', 'checkpoint': checkpoint,
                                        'messages': []}), {'status': 'ok'})
        with patch.object(session_worker, 'answer_with_cli', return_value='partial answer'):
            replied = self._request({'action': 'message', 'message': 'continue',
                                     'result_details': True})
        self.assertEqual(replied['answer'], 'partial answer')
        self.assertEqual(replied['outcome']['status'], 'partial')
        self.assertEqual(replied['interventionIds'], ['d' * 32])
        self.assertEqual(replied['checkpoint']['outcome']['remaining_work'], ['finish'])


if __name__ == '__main__':
    unittest.main()

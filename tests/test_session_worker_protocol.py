"""Session worker socket protocol: result_details surfaces the harness outcome."""

import base64
import json
import os
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from popot_agents.worker import session_worker
from ax_local.worker import remote_client

ROLE = {'agent': 'test', 'instructions': 'hi', 'tools': []}


class ResultDetailsProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.socket_path = os.path.join(self.temporary.name, 'worker.sock')
        self.servers = []

    def _serve(self, mode='cli'):
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
        env = {'HARNESS_ROLE_JSON': json.dumps(ROLE), 'HARNESS_SESSION_MODE': mode,
               'HARNESS_SOCKET_PATH': self.socket_path}
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

    def test_real_subprocess_timeout_returns_an_error_without_replaying_the_turn(self):
        self._serve(mode='http')

        def timed_out(*args, **kwargs):
            subprocess.run([sys.executable, '-c', 'import time; time.sleep(5)',
                            'private-command-argument'], timeout=.05, check=True)

        encoded = base64.urlsafe_b64encode(json.dumps({
            'action': 'message', 'message': 'continue', 'result_details': True,
        }).encode()).decode()
        with patch.object(session_worker, 'run_http', side_effect=timed_out) as answer:
            result = remote_client.call(encoded)
            answer.assert_called_once()
        self.assertEqual(result['error_type'], 'TimeoutExpired')
        self.assertIn('timed out', result['error'])
        self.assertNotIn('private-command-argument', result['error'])
        self.assertEqual(self._request({'action': 'ping'}), {'status': 'ok'})

    def test_disconnected_message_can_recover_its_result_without_repeating_work(self):
        self._serve(mode='http')
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        request = {'action': 'message', 'message': 'change the repository',
                   'request_id': 'a' * 32, 'result_details': True}

        def answer(*args, **kwargs):
            started.set()
            if not release.wait(2):
                raise RuntimeError('test did not release the worker')
            kwargs['state'].update(outcome={'status': 'partial'},
                                   messages=[{'role': 'tool', 'content': 'work already done'}])
            return 'saved result'

        with patch.object(session_worker, 'run_http', side_effect=answer) as run:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as disconnected:
                disconnected.connect(self.socket_path)
                disconnected.sendall((json.dumps(request) + '\n').encode())
                self.assertTrue(started.wait(1))
                disconnected.shutdown(socket.SHUT_RDWR)
            release.set()
            recovered = self._request({'action': 'recover', 'request_id': request['request_id']})
            self.assertEqual(recovered['answer'], 'saved result')
            self.assertEqual(recovered['checkpoint']['messages'][0]['content'], 'work already done')
            self.assertEqual(self._request(request), recovered)
            collision = self._request({**request, 'message': 'different work'})
            self.assertIn('different input', collision['error'])
            run.assert_called_once()

    def test_recovery_of_unknown_request_never_starts_work(self):
        self._serve(mode='http')
        with patch.object(session_worker, 'run_http') as run:
            result = self._request({'action': 'recover', 'request_id': 'b' * 32})
        self.assertEqual(result['error_type'], 'RecoveryUnavailable')
        run.assert_not_called()

    def test_invalid_request_ids_are_rejected_for_message_and_recover(self):
        self._serve(mode='http')
        with patch.object(session_worker, 'run_http') as run:
            message = self._request({'action': 'message', 'message': 'work', 'request_id': 'nope'})
            recovery = self._request({'action': 'recover', 'request_id': 'nope'})
        self.assertEqual(message, {'error': 'invalid worker request ID'})
        self.assertEqual(recovery, {'error': 'invalid worker request ID'})
        run.assert_not_called()

    def test_ax_reconnect_recovers_over_the_real_worker_socket(self):
        from ax_local.api.ax_runner import AxAgentRunner
        self._serve(mode='http')
        runner = AxAgentRunner(image='worker@sha256:' + 'a' * 64, model='test',
                               base_url='http://localhost:1/v1')
        actions = []

        def drop_response(task, encoded, **kwargs):
            actions.append(json.loads(base64.urlsafe_b64decode(encoded))['action'])
            result = remote_client.call(encoded)
            if len(actions) == 1:
                raise runner._command_error('ssh', subprocess.CompletedProcess([], 1, '',
                    'Error: receiving output stream: rpc error: code = Unavailable desc = EOF'))
            return result

        with patch.object(session_worker, 'run_http', return_value='work completed') as run, \
             patch.object(runner, '_remote_command', side_effect=drop_response), \
             patch('ax_local.api.ax_runner.time.sleep'):
            result = runner.remote('task', {'action': 'message', 'message': 'publish'}, timeout=3)
        self.assertEqual(result, {'answer': 'work completed'})
        self.assertEqual(actions, ['message', 'recover'])
        run.assert_called_once()


if __name__ == '__main__':
    unittest.main()

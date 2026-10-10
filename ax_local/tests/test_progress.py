import base64
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from ax_local.api.ax_runner import AxAgentRunner
from ax_local.worker.remote_client import call, PROGRESS_PREFIX, RESPONSE_PREFIX


class AXProgressTests(unittest.TestCase):
    def runner(self):
        return AxAgentRunner(image='worker@sha256:' + 'a' * 64, model='unused',
                             base_url='http://localhost:1/v1')

    def test_cli_drains_stderr_and_reports_progress_before_final_response(self):
        with tempfile.TemporaryDirectory() as directory:
            signal_file = Path(directory) / 'received'
            message = json.dumps({'event': 'tool_call', 'name': 'read_file', 'arguments': {'path': 'app.py'}})
            script = f'''import sys,time,pathlib
sys.stderr.write('x' * 250000); sys.stderr.flush()
print({PROGRESS_PREFIX + json.dumps(message)!r}, flush=True)
deadline=time.monotonic()+2
while not pathlib.Path({str(signal_file)!r}).exists() and time.monotonic()<deadline: time.sleep(.01)
print({RESPONSE_PREFIX + json.dumps({'answer':'done'})!r}, flush=True)
'''
            process = subprocess.Popen([sys.executable, '-u', '-c', script], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, start_new_session=True)
            received = []
            def notify(message):
                received.append(message)
                signal_file.touch()
            with patch('ax_local.api.ax_runner.subprocess.Popen', return_value=process):
                result = self.runner()._remote_command('task', 'encoded', timeout=3, on_progress=notify)
            self.assertEqual(received, [message])
            self.assertEqual(result, {'answer': 'done'})
            self.assertTrue(signal_file.exists())
            self.assertTrue(process.stdout.closed and process.stderr.closed)

    def test_cli_timeout_kills_and_reaps_process_without_leaking_arguments(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        started = time.monotonic()
        with patch('ax_local.api.ax_runner.subprocess.Popen', return_value=process):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                self.runner()._remote_command('task', 'PRIVATE', timeout=.15, on_progress=lambda _: None)
        self.assertLess(time.monotonic() - started, 2)
        self.assertNotIn('PRIVATE', str(caught.exception))
        self.assertIsNotNone(process.poll())
        self.assertTrue(process.stdout.closed and process.stderr.closed)

    def test_socket_reader_handles_coalesced_progress_and_fragmented_final_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'socket')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(path)
                server.listen()
                def respond():
                    with server.accept()[0] as connection:
                        connection.recv(8192)
                        body = (json.dumps({'progress': 'PRIVATE'}) + '\n' +
                                json.dumps({'progress': json.dumps({'event': 'tool_call', 'name': 'read_file'})}) + '\n' +
                                json.dumps({'answer': 'готово'}, ensure_ascii=False) + '\n').encode()
                        connection.sendall(body[:-3])
                        time.sleep(.01)
                        connection.sendall(body[-3:])
                thread = threading.Thread(target=respond)
                thread.start()
                payload = base64.urlsafe_b64encode(json.dumps({'action':'message','message':'work','progress':True}).encode()).decode()
                output = io.StringIO()
                with patch.dict('os.environ', {'HARNESS_SOCKET_PATH': path}), redirect_stdout(output):
                    result = call(payload)
                thread.join(timeout=2)
        self.assertEqual(result, {'answer': 'готово'})
        self.assertNotIn('PRIVATE', output.getvalue())
        self.assertIn(PROGRESS_PREFIX, output.getvalue())


if __name__ == '__main__':
    unittest.main()

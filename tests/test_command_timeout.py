"""A timed-out shell must not leave its command writing in the workspace."""
import os
import shlex
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from popot_agents import tools


class CommandTimeoutTests(unittest.TestCase):
    def run_child(self, *, background=False, interrupt=False):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
            root = Path(directory)
            if os.geteuid() == 0:
                os.chown(root, 10001, 10001)
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            listener.settimeout(5)
            script = (
                "import socket; from pathlib import Path; "
                f"s=socket.create_connection({listener.getsockname()!r}, timeout=5); "
                "s.settimeout(5); print('child ready', flush=True); s.sendall(b'ready'); "
                "command=s.recv(16); "
                "Path('late-write').write_text('written') if command == b'write' else None; "
                "s.sendall(b'written') if command == b'write' else None; s.close()"
            )
            command = shlex.join([sys.executable, '-c', script]) + (' &' if background else '; wait')
            results, failures = [], []
            ready = threading.Event()
            process_factory = tools.subprocess.Popen
            if interrupt:
                class InterruptedProcess(process_factory):
                    interrupted = False

                    def wait(self, timeout=None):
                        if not self.interrupted:
                            self.interrupted = True
                            if not ready.wait(5):
                                raise AssertionError('child did not start before interruption')
                            raise KeyboardInterrupt
                        return super().wait(timeout=timeout)

                process_factory = InterruptedProcess

            def execute():
                try:
                    results.append(tools._run_command(['bash', '--noprofile', '--norc', '-c', command],
                                                      timeout=1))
                except BaseException as exc:
                    failures.append(exc)

            with patch.object(tools, 'WORKSPACE_ROOT', root), \
                 patch.object(tools.subprocess, 'Popen', process_factory):
                thread = threading.Thread(target=execute)
                thread.start()
                try:
                    connection, _ = listener.accept()
                    with connection:
                        connection.settimeout(5)
                        self.assertEqual(connection.recv(16), b'ready')
                        ready.set()
                        thread.join(3)
                        self.assertFalse(thread.is_alive(), 'command did not settle within its deadline')
                        if background:
                            self.assertEqual(failures, [])
                            self.assertTrue(results[0].startswith('exit_code=0\n'))
                        else:
                            self.assertEqual(len(failures), 1)
                            self.assertIsInstance(failures[0], KeyboardInterrupt if interrupt else tools.ToolExecutionError)
                            if not interrupt:
                                self.assertIn('child ready', str(failures[0]))
                        try:
                            connection.sendall(b'write')
                            response = connection.recv(16)
                        except (BrokenPipeError, ConnectionResetError):
                            response = b''
                        self.assertEqual(response, b'written' if background else b'')
                        self.assertEqual((root / 'late-write').exists(), background)
                finally:
                    # The child has a bounded socket read and exits when the peer
                    # closes, including when this regression fails on old code.
                    thread.join(6)

    def test_process_group_gone_at_the_deadline_still_reports_the_timeout(self):
        process = MagicMock()
        process.pid = 12345
        process.__enter__.return_value = process
        process.returncode = -9
        process.wait.side_effect = tools.subprocess.TimeoutExpired('cmd', 1)
        with patch.object(tools.subprocess, 'Popen', return_value=process), \
                patch.object(tools.os, 'killpg', side_effect=ProcessLookupError):
            with self.assertRaises(tools.ToolExecutionError):
                tools._run_command(['true'], timeout=1)

    def test_timeout_stops_child_before_another_command_can_write(self):
        self.run_child()

    def test_successful_shell_can_leave_an_intentional_background_process(self):
        self.run_child(background=True)

    def test_interrupted_wait_also_stops_the_child(self):
        self.run_child(interrupt=True)

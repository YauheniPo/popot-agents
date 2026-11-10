"""The copyable MCP command must report the task outcome, not just curl's status."""

import json
import os
from pathlib import Path
import pty
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'ax_local' / 'run-mcp-task.sh'


CAPTURE_CURL = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
data = args[args.index('--data-binary') + 1]
payload = json.loads(sys.stdin.read() if data == '@-' else data)
with open(os.environ['REQUEST_CAPTURE'], 'w') as output:
    json.dump(payload, output)
print('data: ' + json.dumps({'id': 1, 'result': {'content': [{
    'type': 'text', 'text': json.dumps({'sessionId': 'parent-session',
    'status': 'queued', 'interventionId': 'comment-id'})}]}}))
'''


class RunMcpTaskTests(unittest.TestCase):
    def test_message_formats_remain_one_literal_task_string(self):
        messages = [
            '{"role":"system","task":"replace me","messages":[1,2]}',
            "```json\n{\"message\": \"it's valid\"}\n```\n''' \"\"\"",
            '<message attr="\'quoted\'">&amp; \\u0000</message>',
            '\ufeffРусский / 日本語 / 😀 / e\u0301\r\n\t\b\f\x1b[31m\n\n',
            '\\n is literal; $HOME $(echo unsafe) `echo unsafe` %s @- TASK_TEXT',
            '\x01' * 30000,
        ]
        with tempfile.TemporaryDirectory() as directory:
            curl = Path(directory) / 'curl'
            curl.write_text(CAPTURE_CURL)
            curl.chmod(0o755)
            capture = Path(directory) / 'request.json'
            environment = {**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'],
                           'ROLE': '', 'SESSION_ID': 'parent-session',
                           'REQUEST_CAPTURE': str(capture)}
            for message in messages:
                for source in ('TASK', 'stdin'):
                    with self.subTest(message=repr(message[:60]), source=source):
                        environment['TASK'] = message if source == 'TASK' else ''
                        run = subprocess.run(['bash', str(SCRIPT)], cwd=ROOT, env=environment,
                                             input=message if source == 'stdin' else '',
                                             text=True, capture_output=True, timeout=10)
                        self.assertEqual(run.returncode, 0, run.stderr)
                        arguments = json.loads(capture.read_text())['params']['arguments']
                        self.assertEqual(arguments['task'], message)
                        self.assertEqual(arguments['sessionId'], 'parent-session')
                        self.assertNotIn('messages', arguments)

    def test_invalid_text_is_rejected_without_losing_characters(self):
        with tempfile.TemporaryDirectory() as directory:
            curl = Path(directory) / 'curl'
            curl.write_text(CAPTURE_CURL)
            curl.chmod(0o755)
            capture = Path(directory) / 'request.json'
            environment = {**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'],
                           'TASK': '', 'REQUEST_CAPTURE': str(capture)}
            for data, error in [(b'before\0after', 'NUL'),
                                (b'invalid\xff', 'UTF-8'),
                                (b' \n\t', 'non-whitespace')]:
                with self.subTest(error=error):
                    capture.unlink(missing_ok=True)
                    run = subprocess.run(['bash', str(SCRIPT)], cwd=ROOT, env=environment,
                                         input=data, capture_output=True, timeout=10)
                    self.assertNotEqual(run.returncode, 0)
                    self.assertIn(error, run.stderr.decode(errors='replace'))
                    self.assertFalse(capture.exists())

    def test_printed_command_replays_the_same_message_and_request_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            curl = Path(directory) / 'curl'
            curl.write_text(CAPTURE_CURL)
            curl.chmod(0o755)
            capture = Path(directory) / 'request.json'
            environment = {**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'],
                           'TASK': '', 'SESSION_ID': 'parent-session',
                           'REQUEST_CAPTURE': str(capture)}
            message = "Русский 😀 ''' \"\"\" $HOME $(echo unsafe)\r\n" + '\x01' * 29000
            run = subprocess.run(['bash', str(SCRIPT)], cwd=ROOT, env=environment,
                                 input=message, text=True, capture_output=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            original = json.loads(capture.read_text())
            command = (run.stdout.split('Command to execute:\n', 1)[1]
                       .split('\nPOPOT_MCP_REQUEST\n', 1)[0] + '\nPOPOT_MCP_REQUEST\n')
            replay = subprocess.run(['bash'], cwd=ROOT, env=environment, input=command,
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(replay.returncode, 0, replay.stderr)
            self.assertEqual(json.loads(capture.read_text()), original)
            self.assertEqual(original['params']['arguments']['task'], message)

    def test_pasted_task_is_forwarded_without_shell_expansion(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory) / 'request.json'
            marker = Path(directory) / 'must-not-exist'
            fake_curl = Path(directory) / 'curl'
            fake_curl.write_text(CAPTURE_CURL)
            fake_curl.chmod(0o755)
            environment = {**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'],
                           'ROLE': '', 'SESSION_ID': 'parent-session',
                           'REQUEST_CAPTURE': str(capture)}
            environment.pop('TASK', None)
            task = ("Исправь RuntimeError('turn deadline exceeded')\n\n"
                    "```python\ntext = '''quotes'''\n```\n"
                    f"$(touch {marker}) `touch {marker}` $HOME \\\n\n")
            for configured, terminal in ((None, False), (None, True),
                                         ('task from environment', False)):
                with self.subTest(configured=configured, terminal=terminal):
                    if configured is not None:
                        environment['TASK'] = configured
                    if terminal:
                        master, slave = pty.openpty()
                        process = subprocess.Popen(['bash', str(SCRIPT)], cwd=ROOT,
                                                   env=environment, stdin=slave,
                                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                   text=True)
                        os.close(slave)
                        try:
                            self.assertIn('Ctrl+D', process.stderr.readline())
                            os.write(master, task.encode() + b'\x04')
                            _, stderr = process.communicate(timeout=5)
                            self.assertEqual(process.returncode, 0, stderr)
                        finally:
                            if process.poll() is None:
                                process.kill()
                                process.communicate()
                            os.close(master)
                    else:
                        run = subprocess.run(['bash', str(SCRIPT)], cwd=ROOT, env=environment,
                                             input=task, text=True, capture_output=True)
                        self.assertEqual(run.returncode, 0, run.stderr)
                    arguments = json.loads(capture.read_text())['params']['arguments']
                    self.assertEqual(arguments['task'], task if configured is None else configured)
                    self.assertEqual(arguments['sessionId'], 'parent-session')
                    self.assertFalse(marker.exists())

    def test_empty_stdin_is_rejected_before_sending_a_request(self):
        environment = dict(os.environ)
        environment.pop('TASK', None)
        run = subprocess.run(['bash', str(SCRIPT)], cwd=ROOT, env=environment,
                             input='', text=True, capture_output=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('Set TASK or provide the request text on stdin', run.stderr)
        self.assertNotIn('Command to execute:', run.stdout)

    def test_one_line_command_reports_success_and_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_curl = Path(directory) / 'curl'
            fake_curl.write_text('''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
data = args[args.index('--data-binary') + 1]
payload = json.loads(sys.stdin.read() if data == '@-' else data)
with open(os.environ['REQUEST_CAPTURE'], 'w') as output:
    json.dump(payload, output)
mode = os.environ['MOCK_MCP_MODE']
if mode == 'transport_error':
    sys.exit(7)
if mode == 'mcp_error':
    result = {'content': [{'type': 'text', 'text': 'orchestrator HTTP 502'}], 'isError': True}
else:
    task = ({'sessionId': 'new-session', 'status': 'queued', 'interventionId': 'comment-id'}
            if mode == 'queued' else
            {'sessionId': 'new-session', 'outcome': {'status': mode, 'reason': 'workflow_turn_limit'}})
    result = {'content': [{'type': 'text', 'text': json.dumps(task)}], 'isError': False}
print('event: message')
print('data: ' + json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': result}))
''')
            fake_curl.chmod(0o755)
            for mode, expected, role, session, disable in [
                ('completed', 0, '', '', ''),
                ('blocked', 1, 'backend_engineer', '', ''),
                ('mcp_error', 1, 'backend_engineer', '', ''),
                ('transport_error', 7, 'backend_engineer', '', ''),
                ('completed', 0, 'backend_engineer', 'existing-session', ''),
                ('queued', 0, '', 'existing-session', ''),
                ('completed', 0, 'backend_engineer', '', 'true'),
                ('completed', 0, 'backend_engineer', '', 'false'),
            ]:
                with self.subTest(mode=mode, session=session):
                    capture = Path(directory) / 'request.json'
                    environment = {**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'],
                                   'TASK': 'prepare PR #92', 'ROLE': role,
                                   'SESSION_ID': session, 'CALL_ID': 'stable-call',
                                   'PROGRESS_TOKEN': 'progress-token', 'MOCK_MCP_MODE': mode,
                                   'DISABLE_DELEGATION': disable,
                                   'REQUEST_CAPTURE': str(capture)}
                    run = subprocess.run(['bash', str(SCRIPT)], cwd=ROOT, env=environment,
                                         text=True, capture_output=True)
                    self.assertEqual(run.returncode, expected, run.stderr)
                    self.assertIn('Command to execute:', run.stdout)
                    payload = json.loads(capture.read_text())
                    self.assertEqual(payload['params']['arguments']['task'], 'prepare PR #92')
                    self.assertEqual(payload['params']['arguments'].get('role'),
                                     'backend_engineer' if role and not session else None)
                    self.assertEqual(payload['params']['arguments']['sessionId'], session)
                    self.assertEqual(payload['params']['arguments'].get('disableDelegation'),
                                     None if not disable else disable == 'true')
                    self.assertEqual(payload['params']['_meta'],
                                     {'callId': 'stable-call', 'progressToken': 'progress-token'})
                    if mode == 'blocked':
                        self.assertIn('Task new-session: blocked (workflow_turn_limit)', run.stderr)
                    if mode == 'completed':
                        self.assertIn('Task new-session: completed', run.stderr)
                    if mode == 'queued':
                        self.assertIn('Comment new-session: queued (interventionId=comment-id)', run.stderr)

    def test_unicode_task_and_reply_work_with_c_locale(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_curl = Path(directory) / 'curl'
            fake_curl.write_text('''#!/usr/bin/env python3
import json, sys
sys.stdout.reconfigure(encoding='utf-8')
print('event: message')
print('data: ' + json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {
    'content': [{'type': 'text', 'text': json.dumps({
        'sessionId': 'new-session', 'answer': 'one\u2028two\u0085three\u2029four',
        'outcome': {'status': 'completed'}}, ensure_ascii=False)}],
    'isError': False}}, ensure_ascii=False))
''')
            fake_curl.chmod(0o755)
            environment = {**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'],
                           'TASK': 'подготовь PR #92', 'ROLE': '', 'SESSION_ID': '',
                           'LC_ALL': 'C'}
            for utf8 in ('1', '0'):
                with self.subTest(python_utf8=utf8):
                    environment.update(PYTHONUTF8=utf8, PYTHONCOERCECLOCALE='0')
                    run = subprocess.run(['bash', str(SCRIPT)], cwd=ROOT, env=environment,
                                         text=True, capture_output=True)
                    self.assertEqual(run.returncode, 0, run.stderr)
                    self.assertIn('подготовь PR #92', run.stdout)


if __name__ == '__main__':
    unittest.main()

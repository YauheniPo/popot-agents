"""The copyable MCP command must report the task outcome, not just curl's status."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'ax_local' / 'run-mcp-task.sh'


class RunMcpTaskTests(unittest.TestCase):
    def test_one_line_command_reports_success_and_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_curl = Path(directory) / 'curl'
            fake_curl.write_text('''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
payload = json.loads(args[args.index('--data-binary') + 1])
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
            for mode, expected, role, session in [
                ('completed', 0, '', ''),
                ('blocked', 1, 'backend_engineer', ''),
                ('mcp_error', 1, 'backend_engineer', ''),
                ('transport_error', 7, 'backend_engineer', ''),
                ('completed', 0, 'backend_engineer', 'existing-session'),
                ('queued', 0, '', 'existing-session'),
            ]:
                with self.subTest(mode=mode, session=session):
                    capture = Path(directory) / 'request.json'
                    environment = {**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'],
                                   'TASK': 'prepare PR #92', 'ROLE': role,
                                   'SESSION_ID': session, 'CALL_ID': 'stable-call',
                                   'PROGRESS_TOKEN': 'progress-token', 'MOCK_MCP_MODE': mode,
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
                    self.assertEqual(payload['params']['_meta'],
                                     {'callId': 'stable-call', 'progressToken': 'progress-token'})
                    if mode == 'blocked':
                        self.assertIn('Task new-session: blocked (workflow_turn_limit)', run.stderr)
                    if mode == 'completed':
                        self.assertIn('Task new-session: completed', run.stderr)
                    if mode == 'queued':
                        self.assertIn('Comment new-session: queued (interventionId=comment-id)', run.stderr)

    def test_printed_command_keeps_unicode_task_with_c_locale(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_curl = Path(directory) / 'curl'
            fake_curl.write_text('''#!/usr/bin/env python3
import json
print('event: message')
print('data: ' + json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {
    'content': [{'type': 'text', 'text': json.dumps({
        'sessionId': 'new-session', 'outcome': {'status': 'completed'}})}], 'isError': False}}))
''')
            fake_curl.chmod(0o755)
            environment = {**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'],
                           'TASK': 'подготовь PR #92', 'ROLE': '', 'SESSION_ID': '',
                           'LC_ALL': 'C'}
            run = subprocess.run(['bash', str(SCRIPT)], cwd=ROOT, env=environment,
                                 text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn('подготовь PR #92', run.stdout)


if __name__ == '__main__':
    unittest.main()

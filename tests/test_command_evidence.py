"""Command evidence survives preview limits without rerunning checks."""
import json
import re
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from popot_agents import tools
from popot_agents.worker.http_harness import _trace_arguments


class CommandEvidenceTests(unittest.TestCase):
    def run_command(self, script, **kwargs):
        return tools.execute_tool('bash', {'command':
            shlex.join([sys.executable, '-c', script])}, ['bash'], **kwargs)

    def saved_path(self, result):
        match = re.search(r'Full output: (\.tool-results/[a-f0-9]{32}\.txt)', result)
        self.assertIsNotNone(match, result[-500:])
        return match.group(1)

    def test_failed_check_keeps_full_unicode_output_and_runs_once(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tools, 'WORKSPACE_ROOT', Path(directory)):
            content = 'я' * (tools.MAX_TOOL_OUTPUT + 100) + '\nFAIL: test_parser\nValueError: invalid record\n'
            result = self.run_command(
                "from pathlib import Path; import sys; p=Path('runs'); "
                "p.write_text(p.read_text()+'x' if p.exists() else 'x'); "
                f"sys.stdout.write('я'*{tools.MAX_TOOL_OUTPUT + 100}+'\\nFAIL: test_parser\\nValueError: invalid record\\n'); sys.exit(1)")
            self.assertTrue(result.startswith('exit_code=1\n'))
            path = self.saved_path(result)
            self.assertEqual(Path(directory, path).read_text(), content)
            page = tools.execute_tool('read_tool_output', {'path': path,
                'offset': tools.MAX_TOOL_OUTPUT + 100}, ['read_tool_output'])
            self.assertIn('ValueError: invalid record', page)
            self.assertEqual(Path(directory, 'runs').read_text(), 'x')
            self.assertLess(len(result), tools.MAX_TOOL_OUTPUT + 600)

    def test_small_success_is_saved_too(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tools, 'WORKSPACE_ROOT', Path(directory)):
            result = self.run_command("print('2 passed')")
            self.assertTrue(result.startswith('exit_code=0\n2 passed\n'))
            self.assertEqual(Path(directory, self.saved_path(result)).read_text(), '2 passed\n')

    def test_timeout_preserves_partial_output_as_recoverable_failure(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tools, 'WORKSPACE_ROOT', Path(directory)):
            with self.assertRaises(tools.ToolExecutionError) as caught:
                self.run_command("import time; print('started', flush=True); time.sleep(5)", timeout_seconds=0.2)
            path = self.saved_path(str(caught.exception))
            self.assertEqual(Path(directory, path).read_text(), 'started\n')
            self.assertIn('timed out', str(caught.exception))

    def test_non_utf8_command_output_is_readable(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tools, 'WORKSPACE_ROOT', Path(directory)):
            result = self.run_command("import sys; sys.stdout.buffer.write(b'bad:\\xff\\n')")
            self.assertIn('bad:�', result)
            path = self.saved_path(result)
            self.assertIn('bad:�', tools.execute_tool('read_tool_output', {'path': path}, ['read_tool_output']))

    def test_output_directory_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tools, 'WORKSPACE_ROOT', Path(directory)):
            Path(directory, 'other').mkdir()
            Path(directory, '.tool-results').symlink_to(Path(directory, 'other'), target_is_directory=True)
            with self.assertRaises(tools.ToolExecutionError):
                self.run_command("print('evidence')")
            self.assertEqual(list(Path(directory, 'other').iterdir()), [])

    def test_paging_offsets_preserve_carriage_returns(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tools, 'WORKSPACE_ROOT', Path(directory)):
            result = self.run_command("import sys; sys.stdout.buffer.write(b'a\\r\\nb\\r\\nc')")
            path = self.saved_path(result)
            page = tools.execute_tool('read_tool_output', {'path': path, 'offset': 3}, ['read_tool_output'])
            self.assertEqual(page, 'b\r\nc')

    def test_timeout_output_reaches_model_but_not_progress_trace(self):
        from tests.test_agent_reliability import AgentReliabilityTests
        helper = AgentReliabilityTests()
        failure = tools.ToolExecutionError('command timed out; partial output: private-output')
        with patch('popot_agents.worker.http_harness.execute_tool', side_effect=failure), \
             patch('popot_agents.worker.http_harness._trace') as trace:
            _, _, bodies = helper.run_sequence([
                {'tool_calls': [{'id': 'shell', 'function': {'name': 'bash',
                    'arguments': json.dumps({'command': 'run-check'})}}]}, helper.result()],
                role={'tools': ['bash']})
        self.assertIn('private-output', str(bodies[1]['messages']))
        self.assertNotIn('private-output', str(trace.call_args_list))

    def test_trace_identifies_repeated_commands_without_logging_content(self):
        first = _trace_arguments('bash', {'command': 'echo private-token'})
        self.assertEqual(first['command_sha256'], _trace_arguments('bash', {'command': 'echo private-token'})['command_sha256'])
        self.assertNotEqual(first['command_sha256'], _trace_arguments('bash', {'command': 'echo different'})['command_sha256'])
        self.assertNotIn('private-token', str(first))

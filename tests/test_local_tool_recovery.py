import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from popot_agents import tools
from popot_agents.worker.http_harness import run_http


class LocalToolRecoveryTests(unittest.TestCase):
    def run_turn(self, directories, *, allowed=None, rounds=4, command_error=None):
        self.requests = []
        responses = [
            {'tool_calls': [{'id': f'call-{index}', 'type': 'function', 'function': {
                'name': 'git_clone', 'arguments': json.dumps({
                    'url': 'https://github.com/example/repo.git', 'directory': directory})}}]}
            for index, directory in enumerate(directories)
        ] + [{'content': 'done'}]

        def model_response(call, **kwargs):
            self.requests.append(json.loads(call.data))
            return io.BytesIO(json.dumps({'choices': [{'message': responses.pop(0)}]}).encode())

        with tempfile.TemporaryDirectory() as workspace, \
             patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1', 'HARNESS_MODEL': 'fake'},
                        clear=True), \
             patch.object(tools, 'WORKSPACE_ROOT', Path(workspace)), \
             patch.object(tools, '_run_command', return_value='exit_code=0\n',
                          side_effect=command_error) as command, \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=model_response):
            self.command = command
            return run_http('clone repository', {'tools': ['git_clone'] if allowed is None else allowed,
                                                  'max_tool_rounds': rounds})

    def test_model_corrects_directory_after_rejected_call(self):
        for invalid in ('/workspace/repo', '../repo', 'nested/repo', '.', ''):
            with self.subTest(directory=invalid):
                self.assertEqual(self.run_turn([invalid, 'repo']), 'done')
                self.command.assert_called_once()
                failed = self.requests[1]['messages'][-1]
                self.assertEqual(failed['role'], 'tool')
                self.assertEqual(failed['tool_call_id'], 'call-0')
                result = json.loads(failed['content'])
                self.assertTrue(result['isError'])
                self.assertIn('directory', result['error'])
                self.assertIn('cloned to', self.requests[2]['messages'][-1]['content'])

    def test_clone_schema_explains_and_constrains_directory(self):
        schema = tools.TOOL_SCHEMAS['git_clone']['function']['parameters']['properties']['directory']
        self.assertIn('workspace', schema.get('description', '').lower())
        self.assertEqual(schema.get('maxLength'), 80)
        self.assertEqual(schema.get('minLength'), 1)
        pattern = schema.get('pattern', '.*')
        for value in ('repo', 'popot-agents', 'repo.v2_1'):
            self.assertIsNotNone(re.fullmatch(pattern, value))
        for value in ('../repo', '/workspace/repo', 'nested/repo', '', 'x' * 81):
            self.assertIsNone(re.fullmatch(pattern, value))

    def test_repeated_invalid_arguments_exhaust_round_budget(self):
        with self.assertRaisesRegex(RuntimeError, 'tool-call limit'):
            self.run_turn(['../repo', '../repo'], rounds=2)
        self.command.assert_not_called()
        self.assertEqual(len(self.requests), 2)

    def test_disallowed_tool_remains_fatal_and_never_executes(self):
        with self.assertRaisesRegex(RuntimeError, 'not allowed'):
            self.run_turn(['repo'], allowed=[])
        self.command.assert_not_called()
        self.assertEqual(len(self.requests), 1)

    def test_execution_failure_is_not_retried(self):
        for failure in (RuntimeError('command timed out'), ValueError('invalid runtime config')):
            with self.subTest(failure=str(failure)):
                with self.assertRaisesRegex(RuntimeError, str(failure)):
                    self.run_turn(['repo'], command_error=failure)
                self.command.assert_called_once()
                self.assertEqual(len(self.requests), 1)

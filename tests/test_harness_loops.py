import io
import json
import os
import subprocess
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from popot_agents import tools
from popot_agents.runtime_config import RUNTIME
from popot_agents.orchestrator.main import DockerAgentRunner
from popot_agents.worker.agent_worker import run_harness
from popot_agents.worker.http_harness import run_http


def tool_call(identifier, name, arguments):
    return {'id': identifier, 'type': 'function', 'function': {
        'name': name, 'arguments': arguments if isinstance(arguments, str) else json.dumps(arguments)}}


class HttpRecoveryLoopTests(unittest.TestCase):
    def test_round_budget_uses_runtime_unless_role_overrides_it(self):
        reply = {'choices': [{'message': {'tool_calls': [
            tool_call('calc', 'calculate', {'expression': '1+1'})]}}]}
        for role, expected in (({}, 2), ({'max_tool_rounds': 3}, 3)):
            with self.subTest(role=role), \
                 patch.dict(RUNTIME['model'], {'default_max_tool_rounds': 2}), \
                 patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1', 'HARNESS_MODEL': 'fake'}, clear=True), \
                 patch('popot_agents.worker.http_harness.request.urlopen',
                       side_effect=lambda *a, **k: io.BytesIO(json.dumps(reply).encode())) as model, \
                 redirect_stderr(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'tool-call limit'):
                    run_http('task', {'tools': ['calculate'], **role})
                self.assertEqual(model.call_count, expected)

    def run_messages(self, messages, allowed):
        self.requests = []
        def reply(call, **kwargs):
            self.requests.append(json.loads(call.data))
            return io.BytesIO(json.dumps({'choices': [{'message': messages.pop(0)}]}).encode())
        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1', 'HARNESS_MODEL': 'fake'}, clear=True), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=reply), \
             redirect_stderr(io.StringIO()):
            return run_http('task', {'tools': allowed, 'max_tool_rounds': 4})

    def test_invalid_arguments_do_not_discard_other_tool_results(self):
        for arguments in ('{bad-json', '[]', {'expression': '1/0'}, {'expression': 5}):
            with self.subTest(arguments=arguments):
                answer = self.run_messages([
                    {'tool_calls': [tool_call('ok', 'calculate', {'expression': '2+1'}),
                                    tool_call('bad', 'calculate', arguments)]},
                    {'tool_calls': [tool_call('fixed', 'calculate', {'expression': '1+1'})]},
                    {'content': 'done'}], ['calculate'])
                self.assertEqual(answer, 'done')
                results = self.requests[1]['messages'][-2:]
                self.assertEqual(results[0]['content'], '3')
                self.assertEqual(results[1]['tool_call_id'], 'bad')
                self.assertTrue(json.loads(results[1]['content'])['isError'])
                self.assertEqual(self.requests[2]['messages'][-1]['content'], '2')

    def test_failed_file_operation_can_be_repaired_by_next_step(self):
        failure = subprocess.CompletedProcess([], 2, '', 'file not found')
        success = subprocess.CompletedProcess([], 0, 'file contents', '')
        with patch.object(tools, 'subprocess') as process:
            process.run.side_effect = [failure, success]
            self.assertEqual(self.run_messages([
                {'tool_calls': [tool_call('missing', 'read_file', {'path': 'missing.txt'})]},
                {'tool_calls': [tool_call('fixed', 'read_file', {'path': 'README.md'})]},
                {'content': 'done'}], ['read_file']), 'done')
        self.assertTrue(json.loads(self.requests[1]['messages'][-1]['content'])['isError'])

    def test_invalid_input_to_each_local_tool_is_recoverable_before_execution(self):
        cases = [('bash', {'command': 7}), ('read_file', {'path': '../escape'}),
                 ('write_file', {'path': 'x', 'content': 2}),
                 ('download_file', {'url': 'file:///etc/passwd', 'path': 'x'}),
                 ('utc_time', {'extra': True})]
        for name, arguments in cases:
            with self.subTest(name=name), patch.object(tools, '_run_command') as command, \
                 patch.object(tools, '_file_action') as file_action:
                self.assertEqual(self.run_messages([
                    {'tool_calls': [tool_call('bad', name, arguments)]},
                    {'content': 'Need a valid argument.'}], [name]), 'Need a valid argument.')
                self.assertTrue(json.loads(self.requests[1]['messages'][-1]['content'])['isError'])
                command.assert_not_called()
                file_action.assert_not_called()


class CliLoopBudgetTests(unittest.TestCase):
    def test_cli_inherits_runtime_rounds_when_role_omits_override(self):
        with patch.dict(RUNTIME['model'], {'default_max_tool_rounds': 7}), \
             patch.dict(os.environ, {
                 'HARNESS_COMMAND_JSON': '["claude", "--print", "--max-turns", "{max_turns}"]',
             }, clear=True), patch('popot_agents.worker.agent_worker.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, 'done', '')
            self.assertEqual(run_harness('task'), 'done')
            self.assertEqual(run.call_args.args[0][-1], '7')

    def test_configured_claude_profiles_bound_the_native_loop(self):
        config = json.loads((Path(__file__).resolve().parents[1] / 'config/agents.json').read_text())
        for name in ('ollama_claude', 'ollama_claude_local'):
            self.assertIn('--max-turns', config[name]['command'])
            self.assertIn('{max_turns}', config[name]['command'])

    def test_cli_gets_role_round_budget_and_turn_timeout(self):
        with patch.dict(os.environ, {
            'HARNESS_COMMAND_JSON': '["claude", "--print", "--max-turns", "{max_turns}", "{task}"]',
            'HARNESS_ROLE_JSON': '{"max_tool_rounds":12}', 'HARNESS_TURN_TIMEOUT_SECONDS': '17',
        }, clear=True), patch('popot_agents.worker.agent_worker.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, 'done', '')
            self.assertEqual(run_harness('task'), 'done')
            self.assertEqual(run.call_args.args[0], ['claude', '--print', '--max-turns', '12', 'task'])
            self.assertEqual(run.call_args.kwargs['timeout'], 17)

    def test_cli_timeout_is_reported_without_restarting_process(self):
        with patch.dict(os.environ, {'HARNESS_COMMAND_JSON': '["example"]'}, clear=True), \
             patch('popot_agents.worker.agent_worker.subprocess.run',
                   side_effect=subprocess.TimeoutExpired('example', 60)) as run:
            with self.assertRaisesRegex(RuntimeError, 'CLI harness turn timed out'):
                run_harness('task')
            run.assert_called_once()

    def test_docker_passes_cli_turn_budget(self):
        runner = DockerAgentRunner(image='example', session_mode='cli', timeout_seconds=180)
        command = runner._docker_command('popot-chat-test', True)
        self.assertIn('HARNESS_TURN_TIMEOUT_SECONDS=175', command)

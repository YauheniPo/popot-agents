import io
import importlib.util
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from popot_agents.orchestrator.main import load_roles
from popot_agents.worker.http_harness import run_http


class Harness2RoleTests(unittest.TestCase):
    def test_role_opt_in_is_validated_and_disabled_by_default(self):
        profile = type('Profile', (), {'session_mode': 'http'})()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'roles.json'
            role = {'agent': 'test', 'instructions': 'Review evidence.', 'tools': []}
            for setting in ({'enabled': True}, {'enabled': False},
                            {'enabled': True, 'timeout_seconds': 180}):
                role['harness2'] = setting
                path.write_text(json.dumps({'reviewer': role}))
                self.assertEqual(load_roles(path, {'test': profile})['reviewer']['harness2'], setting)
            for setting in (True, {}, {'enabled': 'yes'}, {'enabled': True, 'timeout_seconds': 0},
                            {'enabled': True, 'timeout_seconds': True}, {'enabled': True, 'unknown': 1}):
                with self.subTest(setting=setting):
                    role['harness2'] = setting
                    path.write_text(json.dumps({'reviewer': role}))
                    with self.assertRaisesRegex(ValueError, 'harness2'):
                        load_roles(path, {'test': profile})
            profile.session_mode = 'cli'
            role['harness2'] = {'enabled': True}
            path.write_text(json.dumps({'reviewer': role}))
            with self.assertRaisesRegex(ValueError, 'HTTP'):
                load_roles(path, {'test': profile})

    def test_only_selected_roles_are_enabled_in_project(self):
        roles = json.loads((Path(__file__).resolve().parents[1] / 'config/roles.json').read_text())
        enabled = [name for name, config in roles.items() if config.get('harness2', {}).get('enabled')]
        self.assertEqual(enabled, ['code_reviewer'])

    def test_worker_applies_addendum_once_and_preserves_original_configuration(self):
        from popot_agents.worker.harness2 import refine_role
        role = {'name': 'reviewer', 'instructions': 'Keep the original policy.',
                'tools': [], 'harness2': {'enabled': True, 'timeout_seconds': 5}}
        environment = {'HARNESS_BASE_URL': 'http://model.test/v1', 'HARNESS_MODEL': 'ax-code-free',
                       'HARNESS2_IMPROVER_MODEL': 'ax-harness-free',
                       'HARNESS2_PROXY_TOKEN': 'opaque-improver-token'}
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {**environment, 'HARNESS_SOCKET_PATH': directory + '/chat.sock'}), \
             patch('popot_agents.worker.harness2.subprocess.run', return_value=subprocess.CompletedProcess(
                 [], 0, json.dumps({'prompt': 'Check the full source before reporting a defect.',
                                    'experience': {'lesson': 'Verify surrounding code.'}}), '')) as child:
            updated = refine_role([{'role': 'user', 'content': 'Review this change'}], role,
                                  deadline=time.monotonic() + 20, tool_names=[])
            self.assertIn(role['instructions'], updated['instructions'])
            self.assertIn('Check the full source', updated['instructions'])
            self.assertEqual(role['instructions'], 'Keep the original policy.')
            child.assert_called_once()
            self.assertLessEqual(child.call_args.kwargs['timeout'], 5)
            self.assertNotIn('GITHUB_PERSONAL_ACCESS_TOKEN', child.call_args.kwargs['env'])
            self.assertEqual(list((Path(directory) / 'harness2').iterdir()),
                             [Path(directory) / 'harness2/reviewer'])
            self.assertEqual(list(Path(directory).glob('.harness2-*')), [])

    def test_timeout_or_invalid_candidate_keeps_original_role(self):
        from popot_agents.worker.harness2 import refine_role
        role = {'name': 'qa_engineer', 'instructions': 'Derive test scenarios.',
                'tools': [], 'harness2': {'enabled': True}}
        for outcome in (subprocess.TimeoutExpired('harness2', 1),
                        subprocess.CompletedProcess([], 0, '{invalid', ''),
                        subprocess.CompletedProcess([], 0, json.dumps({'prompt': 'x' * 1601}), '')):
            with self.subTest(outcome=type(outcome).__name__), tempfile.TemporaryDirectory() as directory, \
                 patch.dict(os.environ, {'HARNESS_SOCKET_PATH': directory + '/chat.sock',
                    'HARNESS_MODEL': 'ax-free', 'HARNESS_BASE_URL': 'http://proxy.test/v1',
                    'HARNESS2_IMPROVER_MODEL': 'ax-harness-free', 'HARNESS2_PROXY_TOKEN': 'token'}), \
                 patch('popot_agents.worker.harness2.subprocess.run',
                       **({'side_effect': outcome} if isinstance(outcome, Exception) else {'return_value': outcome})):
                self.assertEqual(refine_role([], role, deadline=time.monotonic() + 5,
                                            tool_names=[]), role)
                self.assertEqual(list(Path(directory).glob('.harness2-*')), [])

    def test_unselected_role_never_starts_optimizer(self):
        from popot_agents.worker.harness2 import refine_role
        with patch('popot_agents.worker.harness2.subprocess.run') as child:
            role = {'name': 'chat', 'instructions': 'Answer briefly.'}
            self.assertEqual(refine_role([], role, deadline=time.monotonic() + 10, tool_names=[]), role)
            child.assert_not_called()

    def test_planning_state_is_scoped_to_role_and_reused_only_for_same_instructions(self):
        from popot_agents.worker.harness2 import refine_role
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'HARNESS_BASE_URL': 'http://proxy.test/v1', 'HARNESS_MODEL': 'ax-free',
                'HARNESS_SOCKET_PATH': directory + '/chat.sock',
                'HARNESS2_IMPROVER_MODEL': 'ax-harness-free', 'HARNESS2_PROXY_TOKEN': 'token'}), \
             patch('popot_agents.worker.harness2.subprocess.run', return_value=subprocess.CompletedProcess(
                 [], 0, json.dumps({'prompt': '', 'experience': {'lesson': 'Check relevant sources.'}}), '')) as child:
            role = {'name': 'reviewer', 'instructions': 'Review.', 'harness2': {'enabled': True}}
            refine_role([], role, deadline=time.monotonic() + 10, tool_names=[])
            refine_role([], role, deadline=time.monotonic() + 10, tool_names=[])
            self.assertIn('lesson', json.loads(child.call_args.kwargs['input'])['experience'])
            refine_role([], {**role, 'name': 'qa_engineer'}, deadline=time.monotonic() + 10, tool_names=[])
            self.assertEqual(json.loads(child.call_args.kwargs['input'])['experience'], {})
            refine_role([], {**role, 'instructions': 'New policy.'}, deadline=time.monotonic() + 10, tool_names=[])
            self.assertEqual(json.loads(child.call_args.kwargs['input'])['experience'], {})

    def test_deadline_terminates_real_child_and_cleans_temporary_directory(self):
        from popot_agents.worker.harness2 import refine_role
        import sys
        actual_run = subprocess.run
        directories = []

        def delayed_child(_args, **kwargs):
            directories.append(json.loads(kwargs['input'])['directory'])
            return actual_run([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)

        role = {'name': 'reviewer', 'instructions': 'Review.',
                'harness2': {'enabled': True, 'timeout_seconds': 1}}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'HARNESS_BASE_URL': 'http://proxy.test/v1', 'HARNESS_MODEL': 'ax-free',
                'HARNESS_SOCKET_PATH': directory + '/chat.sock',
                'HARNESS2_IMPROVER_MODEL': 'ax-harness-free', 'HARNESS2_PROXY_TOKEN': 'token'}), \
             patch('popot_agents.worker.harness2.subprocess.run', side_effect=delayed_child):
            started = time.monotonic()
            self.assertEqual(refine_role([], role, deadline=started + 20, tool_names=[]), role)
            self.assertLess(time.monotonic() - started, 5)
            self.assertTrue(directories)
            self.assertTrue(all(not Path(path).exists() for path in directories))

    def test_http_task_uses_selected_prompt_and_executes_only_once(self):
        role = {'name': 'reviewer', 'instructions': 'Original.', 'tools': [],
                'harness2': {'enabled': True}}
        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://proxy.test/v1',
                                    'HARNESS_MODEL': 'ax-free', 'HARNESS_API_KEY_ENV': ''}), \
             patch('popot_agents.worker.harness2.refine_role',
                   return_value={**role, 'instructions': 'Original. Check source.'}) as refine, \
             patch('urllib.request.urlopen', return_value=io.BytesIO(
                 b'{"choices":[{"message":{"content":"review result"}}]}')) as upstream:
            self.assertEqual(run_http('review', role), 'review result')
        refine.assert_called_once()
        upstream.assert_called_once()
        body = json.loads(upstream.call_args.args[0].data)
        self.assertIn('Check source.', body['messages'][0]['content'])


@unittest.skipUnless(importlib.util.find_spec('harness2'), 'Harness2 requires the Python 3.12 worker environment')
class Harness2NativeTests(unittest.TestCase):
    def test_real_recursion_selection_and_worker_tools_execute_once(self):
        self.run_cycle()

    def test_empty_planning_answer_is_repaired_within_the_request_cap(self):
        self.run_cycle(empty_first=True)

    def test_selector_can_keep_the_original_role(self):
        self.run_cycle(keep_base=True)

    def run_cycle(self, *, empty_first=False, keep_base=False):
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append(body)
                system = body['messages'][0]['content']
                if body['model'] == 'ax-harness-free':
                    if body.get('tools'):
                        self_test.assertEqual(body['tools'][0]['function']['name'], 'propose_prompt')
                        message = {'tool_calls': [{'id': 'proposal', 'type': 'function', 'function': {
                            'name': 'propose_prompt', 'arguments': json.dumps({
                                'prompt': 'Verify the full source before reporting defects.',
                                'notes': 'The initial plan omitted source verification.'})}}]}
                    elif 'Choose using only' in system:
                        evidence = body['messages'][1]['content']
                        candidates = re.findall(r'## candidates/([^/]+)\.md\n(.*?)(?=\n## |\Z)', evidence, re.S)
                        self_test.assertGreaterEqual(len(candidates), 2)
                        expected_plan = 'Plan without a source check.' if keep_base else 'Plan with source verification.'
                        chosen = next(name for name, text in candidates if expected_plan in text)
                        message = {'content': '```choice\n' + chosen + '\n```\n<why>Checks source.</why>'}
                    else:
                        message = {'content': '<experience>cue: code review\nlesson: verify source\n'
                            'evidence: the revised plan adds source checks\nconfidence: planning only</experience>'}
                elif body.get('tools'):
                    self_test.assertEqual(body['tools'][0]['function']['name'], 'calculate')
                    if any(item.get('role') == 'tool' for item in body['messages']):
                        message = {'content': 'Executed result: 4'}
                    else:
                        message = {'tool_calls': [{'id': 'calc', 'type': 'function', 'function': {
                            'name': 'calculate', 'arguments': '{"expression":"2+2"}'}}]}
                else:
                    message = ({'content': None} if empty_first and len(calls) == 1 else
                               {'content': 'Plan with source verification.' if 'Verify the full source' in system
                                else 'Plan without a source check.'})
                answer = json.dumps({'choices': [{'finish_reason': 'stop', 'message': message}]}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

            def log_message(self, *_args):
                pass

        self_test = self
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                    'HARNESS_BASE_URL': f'http://127.0.0.1:{server.server_port}/v1',
                    'HARNESS_MODEL': 'ax-code-free', 'HARNESS_API_KEY_ENV': '',
                    'HARNESS_SOCKET_PATH': directory + '/chat.sock', 'HARNESS_TURN_TIMEOUT_SECONDS': '30',
                    'HARNESS2_IMPROVER_MODEL': 'ax-harness-free', 'HARNESS2_PROXY_TOKEN': 'opaque-token'}):
                role = {'name': 'code_reviewer', 'instructions': 'Review evidence.', 'tools': ['calculate'],
                        'harness2': {'enabled': True, 'timeout_seconds': 10}}
                from popot_agents.worker import http_harness
                with patch.object(http_harness, 'execute_tool', wraps=http_harness.execute_tool) as tool:
                    self.assertEqual(run_http('Review the supplied change', role), 'Executed result: 4')
                tool.assert_called_once()
                state = json.loads((Path(directory) / 'harness2/code_reviewer/state.json').read_text())
                self.assertEqual(state['selected'], not keep_base)
                self.assertIn('verify source', state['experience']['lesson'])
                self.assertEqual(len(calls), 12 if empty_first else 11)
                actual = [body for body in calls if body.get('tools', [{}])[0].get('function', {}).get('name') == 'calculate']
                self.assertEqual(len(actual), 2)
                self.assertEqual('Verify the full source' in actual[0]['messages'][0]['content'], not keep_base)
                self.assertEqual(list(Path(directory).glob('.harness2-*')), [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()

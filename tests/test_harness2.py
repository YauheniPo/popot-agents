import io
import importlib.util
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from contextlib import nullcontext

from popot_agents.orchestrator.main import load_roles
from popot_agents.worker.http_harness import run_http


class Harness2RoleTests(unittest.TestCase):
    def test_role_opt_in_is_validated_and_disabled_by_default(self):
        profile = type('Profile', (), {'session_mode': 'http'})()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'roles.json'
            role = {'agent': 'test', 'instructions': 'Review evidence.', 'tools': []}
            for setting in ({'enabled': True}, {'enabled': False},
                            {'enabled': True, 'timeout_seconds': 180},
                            {'enabled': True, 'mode': 'execution', 'timeout_seconds': 600}):
                role['harness2'] = setting
                path.write_text(json.dumps({'reviewer': role}))
                self.assertEqual(load_roles(path, {'test': profile})['reviewer']['harness2'], setting)
            for setting in (True, {}, {'enabled': 'yes'}, {'enabled': True, 'timeout_seconds': 0},
                            {'enabled': True, 'timeout_seconds': True}, {'enabled': True, 'unknown': 1},
                            {'enabled': True, 'mode': 'invalid'}, {'enabled': True, 'timeout_seconds': 901}):
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
        self.assertEqual(enabled, ['analyst', 'backend_engineer', 'frontend_engineer', 'code_reviewer', 'qa_engineer'])
        self.assertTrue(all(roles[name]['harness2']['mode'] == 'execution' for name in enabled))

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

    def test_execution_mode_preserves_oversized_input_by_skipping_probes(self):
        from popot_agents.worker.harness2 import refine_role
        role = {'name': 'analyst', 'instructions': 'Analyze.',
                'harness2': {'enabled': True, 'mode': 'execution'}}
        with patch.dict(os.environ, {'HARNESS_MODEL': 'ax-free',
                'HARNESS_BASE_URL': 'http://proxy.test/v1',
                'HARNESS2_IMPROVER_MODEL': 'ax-harness-free', 'HARNESS2_PROXY_TOKEN': 'test'}), \
             patch('popot_agents.worker.harness2.subprocess.run') as child:
            self.assertEqual(refine_role([{'role':'user', 'content':'x' * 30001}], role,
                deadline=time.monotonic() + 30, tool_names=[]), role)
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


class PlanningClientTests(unittest.TestCase):
    def setUp(self):
        from popot_agents.worker.harness2 import PlanningClient
        self.client = PlanningClient({
            'base_url': 'http://model.test/v1', 'budget_seconds': 30,
            'model': 'worker-model', 'improver_model': 'ax-harness-free',
            'key': None, 'improver_key': 'planning-token',
            'parameters': {}, 'improver_parameters': {},
        })

    def test_request_cap_and_expired_deadline_prevent_network_calls(self):
        answer = b'{"choices":[{"message":{"content":"plan"}}]}'
        with patch('urllib.request.urlopen', side_effect=lambda *a, **k: io.BytesIO(answer)) as upstream:
            for _ in range(10):
                self.assertEqual(self.client([]), {'content': 'plan'})
            with self.assertRaisesRegex(RuntimeError, 'budget exhausted'):
                self.client([])
            self.assertEqual(upstream.call_count, 10)
            self.client.count = 0
            self.client.deadline = time.monotonic() - 1
            with self.assertRaisesRegex(RuntimeError, 'budget exhausted'):
                self.client([])
            self.assertEqual(upstream.call_count, 10)

    def test_oversized_truncated_and_invalid_responses_are_rejected(self):
        for response, message in (
                (b'x' * 262145, 'too large'),
                (b'{"choices":[{"finish_reason":"length","message":{"content":"partial"}}]}', 'truncated'),
                (b'not-json', 'Expecting value')):
            with self.subTest(message=message), \
                 patch('urllib.request.urlopen', return_value=io.BytesIO(response)):
                with self.assertRaisesRegex(ValueError, message):
                    self.client([])

    def test_proposals_use_improver_capability_and_only_the_prompt_tool(self):
        answer = b'{"choices":[{"message":{"content":"plan"}}]}'
        with patch('urllib.request.urlopen', return_value=io.BytesIO(answer)) as upstream:
            self.client([{'role': 'user', 'content': 'Plan'}], improver=True, proposal=True)
        call = upstream.call_args.args[0]
        body = json.loads(call.data)
        self.assertEqual(call.get_header('Authorization'), 'Bearer planning-token')
        self.assertEqual(body['model'], 'ax-harness-free')
        self.assertEqual(body['max_tokens'], 2048)
        self.assertEqual([tool['function']['name'] for tool in body['tools']], ['propose_prompt'])
        self.assertEqual(body['tool_choice']['function']['name'], 'propose_prompt')
        self.assertGreater(upstream.call_args.kwargs['timeout'], 0)
        self.assertLessEqual(upstream.call_args.kwargs['timeout'], 30)


@unittest.skipUnless(importlib.util.find_spec('harness2'), 'Harness2 requires the Python 3.12 worker environment')
class Harness2NativeTests(unittest.TestCase):
    def test_execution_mode_compares_actual_tool_results(self):
        from popot_agents.worker.harness2 import optimize
        from popot_agents.tools import execute_tool
        calls, probes = [], []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append(body)
                system = body['messages'][0]['content']
                if body.get('tools'):
                    message = {'tool_calls': [{'id': 'edit', 'type': 'function', 'function': {
                        'name': 'propose_prompt', 'arguments': json.dumps({
                            'prompt': 'Verify the result using calculate.', 'notes': 'Use tool evidence.'})}}]}
                elif 'Choose using only' in system:
                    candidates = []
                    for section in ('\n' + body['messages'][1]['content']).split('\n## ')[1:]:
                        heading, separator, _text = section.partition('\n')
                        if separator and heading.startswith('candidates/') and heading.endswith('.md'):
                            label = heading.removeprefix('candidates/').removesuffix('.md')
                            if label and '/' not in label:
                                candidates.append(label)
                    message = {'content': '```choice\n' + candidates[-1] + '\n```\n<why>Verified calculation.</why>'}
                else:
                    message = {'content': '<experience>cue: arithmetic\nlesson: calculate\n'
                        'evidence: tool returned 4\nconfidence: observed</experience>'}
                answer = json.dumps({'choices': [{'message': message}]}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

            def log_message(self, *_args):
                pass

        def probe(task, prompt, **kwargs):
            result = execute_tool('calculate', {'expression': '2+2'}, ['calculate'])
            probes.append((task, prompt, result))
            return {'sessionId': str(len(probes)), 'answer': 'Verified result: ' + result,
                    'activity': [{'seq': 1, 'message': '{"event":"tool_result","output":"4"}'}]}

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory, \
                 patch('ax_local.worker.delegation.run_probe', side_effect=probe):
                result = optimize({'directory': directory, 'role': 'analyst', 'instructions': 'Calculate.',
                    'messages': [{'role': 'user', 'content': 'Compute 2+2 using calculate.'}],
                    'tools': ['calculate'], 'base_url': f'http://127.0.0.1:{server.server_port}/v1',
                    'model': 'solver', 'key': None, 'improver_model': 'improver:free', 'improver_key': 'test',
                    'parameters': {}, 'improver_parameters': {}, 'budget_seconds': 20,
                    'probe': {'AX_HARNESS2_ROLE': 'analyst', 'AX_HARNESS2_URL': 'http://test',
                              'AX_HARNESS2_TOKEN': 'test'}})
            self.assertEqual(len(probes), 4)
            self.assertTrue(all(item[2] == '4' for item in probes))
            self.assertEqual(len(calls), 5)
            self.assertTrue(all(call['model'] == 'improver:free' for call in calls))
            self.assertEqual(len(result['report']['probes']), 4)
            self.assertTrue(all('Verified result: 4' in item['answer'] for item in result['report']['probes']))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_real_recursion_selection_and_worker_tools_execute_once(self):
        self.run_cycle()

    def test_empty_planning_answer_is_repaired_within_the_request_cap(self):
        self.run_cycle(empty_first=True)

    def test_selector_can_keep_the_original_role(self):
        self.run_cycle(keep_base=True)

    def test_optimizer_selects_from_real_sdk_plans_in_process(self):
        for options in ({}, {'empty_first': True}, {'keep_base': True}):
            with self.subTest(options=options):
                self.run_cycle(in_process=True, **options)

    def run_cycle(self, *, empty_first=False, keep_base=False, in_process=False):
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
                        candidates = []
                        for section in ('\n' + evidence).split('\n## ')[1:]:
                            heading, separator, text = section.partition('\n')
                            if separator and heading.startswith('candidates/') and heading.endswith('.md'):
                                label = heading.removeprefix('candidates/').removesuffix('.md')
                                if label and '/' not in label:
                                    candidates.append((label, text))
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
                def execute_planner(command, **kwargs):
                    from popot_agents.worker.harness2 import optimize
                    result = optimize(json.loads(kwargs['input']))
                    return subprocess.CompletedProcess(command, 0, json.dumps(result), '')

                planner = (patch('popot_agents.worker.harness2.subprocess.run', side_effect=execute_planner)
                           if in_process else nullcontext())
                with planner, patch.object(http_harness, 'execute_tool', wraps=http_harness.execute_tool) as tool:
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

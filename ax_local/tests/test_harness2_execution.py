"""Real Harness2 probes must stay within the parent actor's authority."""

import json
import unittest
import time
import threading
from types import SimpleNamespace
from unittest.mock import patch

from ax_local.api.delegation import A2AError, start_a2a_server
from ax_local.api.ax_runner import AxAgentRunner
from ax_local.tests import test_delegation as delegation_tests


class Harness2ExecutionTests(unittest.TestCase):
    wait_task = delegation_tests.DelegationTests.wait_task

    def setUp(self):
        delegation_tests.DelegationTests.setUp(self)
        self.roles['backend_engineer'].update(
            harness2={'enabled': True, 'mode': 'execution', 'timeout_seconds': 300},
            tools=['bash', 'read_file', 'write_file', 'calculate'],
            env_names=['GITHUB_PERSONAL_ACCESS_TOKEN', 'COGNEE_PASSWORD', 'GITGUARDIAN_API_KEY', 'SONAR_TOKEN'],
            mcpServers={'github': {'url': 'https://api.githubcopilot.com/mcp/',
                'bearer_token_env': 'GITHUB_PERSONAL_ACCESS_TOKEN',
                'tools': ['get_file_contents', 'pull_request_read', 'create_pull_request']},
                'cognee': {'command': ['memory'], 'tools': ['remember', 'recall']}})
        self.actor = '1111111111111111'
        self.environment = self.service.worker_environment(self.actor, self.roles['backend_engineer'])

    def probe(self, *, message_id='probe-1', prompt='Verify the regression test.'):
        return self.service.dispatch(self.environment['AX_HARNESS2_TOKEN'], 'backend_engineer',
            'SendMessage', {'message': {'role': 'ROLE_USER', 'messageId': message_id,
                'parts': [{'text': 'Fix the supplied example and run tests.'}],
                'metadata': {'harness2': {'prompt': prompt, 'timeout_seconds': 1}}},
                'configuration': {'returnImmediately': True}})['task']

    def test_probe_is_separate_same_role_actor_without_recursive_or_write_authority(self):
        with self.service.turn(self.actor, 5):
            task = self.probe()
            result = self.wait_task(task['id'], token=self.environment['AX_HARNESS2_TOKEN'],
                                    role='backend_engineer')
        self.assertEqual(result['status']['state'], 'TASK_STATE_COMPLETED')
        probe = self.runner.chats[-1]
        self.assertNotEqual(probe.session_id, self.actor)
        self.assertTrue(probe.closed)
        self.assertEqual(probe.config['harness2'], {'enabled': False})
        self.assertEqual(probe.config['allowed_roles'], [])
        self.assertNotIn('cognee', probe.config['mcpServers'])
        self.assertEqual(probe.config['mcpServers']['github']['tools'],
                         ['get_file_contents', 'pull_request_read'])
        self.assertTrue(probe.config['mcpServers']['github']['url'].endswith('/readonly'))
        self.assertIn('Verify the regression test.', probe.config['instructions'])
        self.assertEqual(self.service.worker_environment(probe.session_id, probe.config), {})
        self.assertEqual(self.roles['backend_engineer']['allowed_roles'], ['qa_engineer', 'tech_lead'])

    def test_probe_retries_are_deduplicated_and_four_probe_limit_is_independent_of_delegation(self):
        with self.service.turn(self.actor, 5):
            first = self.probe()
            self.wait_task(first['id'], token=self.environment['AX_HARNESS2_TOKEN'], role='backend_engineer')
            self.assertEqual(self.probe()['id'], first['id'])
            with self.assertRaises(A2AError):
                self.probe(prompt='Different input')
            for index in range(2, 5):
                task = self.probe(message_id=f'probe-{index}')
                self.wait_task(task['id'], token=self.environment['AX_HARNESS2_TOKEN'], role='backend_engineer')
            with self.assertRaisesRegex(A2AError, 'probe limit'):
                self.probe(message_id='probe-5')
            self.assertEqual(self.service.grants[self.actor].run.calls, 0)

    def test_probe_preserves_parent_instructions_but_respects_current_tool_restrictions(self):
        original = self.roles['backend_engineer']['instructions']
        self.roles['backend_engineer'] = {**self.roles['backend_engineer'],
            'instructions': 'New instructions for new sessions', 'tools': ['read_file', 'calculate']}
        with self.service.turn(self.actor, 5):
            task = self.probe()
            self.wait_task(task['id'], token=self.environment['AX_HARNESS2_TOKEN'], role='backend_engineer')
        config = self.runner.chats[-1].config
        self.assertTrue(config['instructions'].startswith(original))
        self.assertNotIn('New instructions for new sessions', config['instructions'])
        self.assertEqual(config['tools'], ['read_file', 'calculate'])

    def test_probe_token_cannot_delegate_to_other_roles_or_submit_a_normal_task(self):
        with self.service.turn(self.actor, 5):
            with self.assertRaises(A2AError):
                self.service.dispatch(self.environment['AX_HARNESS2_TOKEN'], 'qa_engineer',
                                      'SendMessage', {})
            with self.assertRaises(A2AError):
                self.service.dispatch(self.environment['AX_HARNESS2_TOKEN'], 'backend_engineer',
                    'SendMessage', {'message': {'role': 'ROLE_USER', 'messageId': 'bad',
                        'parts': [{'text': 'ordinary task'}]}})

    def test_full_shared_worker_pool_skips_probes_instead_of_waiting_on_its_parents(self):
        self.runner.worker_queue = SimpleNamespace(status=lambda: {'active': 2, 'limit': 2, 'queued': 0})
        with self.service.turn(self.actor, 5):
            with self.assertRaisesRegex(A2AError, 'worker capacity'):
                self.probe()
        self.assertEqual(self.runner.chats, [])

    def test_only_one_probe_is_active_per_parent_and_other_parents_cannot_read_it(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.runner.callback = lambda chat, task: release.wait(1)
        other = self.service.worker_environment('3333333333333333', self.roles['backend_engineer'])
        with self.service.turn(self.actor, 5), self.service.turn('3333333333333333', 5):
            task = self.probe()
            with self.assertRaisesRegex(A2AError, 'already active'):
                self.probe(message_id='concurrent-probe')
            with self.assertRaisesRegex(A2AError, 'task not found'):
                self.service.dispatch(other['AX_HARNESS2_TOKEN'], 'backend_engineer',
                                      'GetTask', {'id': task['id']})
            release.set()
            self.wait_task(task['id'], token=self.environment['AX_HARNESS2_TOKEN'], role='backend_engineer')

    def test_trial_manifest_keeps_github_credentials_out_of_shell_environment(self):
        with self.service.turn(self.actor, 5):
            task = self.probe()
            self.wait_task(task['id'], token=self.environment['AX_HARNESS2_TOKEN'], role='backend_engineer')
        config = self.runner.chats[-1].config
        runner = AxAgentRunner('image@sha256:' + 'a' * 64, 'test', 'http://model.test/v1')
        with patch.dict('os.environ', {'GITHUB_PERSONAL_ACCESS_TOKEN': 'fake-github-token'}):
            env = {item['name']: item['value'] for item in runner.task_manifest('2222222222222222', config)['spec']['env']}
        env.update(json.loads(env['AX_CAPABILITIES_JSON']))
        self.assertNotIn('GITHUB_PERSONAL_ACCESS_TOKEN', env)
        self.assertEqual(env['AX_HARNESS2_GITHUB_TOKEN'], 'fake-github-token')
        self.assertEqual(json.loads(env['HARNESS_TOOL_ENV_NAMES_JSON']), [])
        self.assertNotIn('HARNESS2_PROXY_TOKEN', env)
        self.assertNotIn('GITGUARDIAN_API_KEY', env)
        self.assertNotIn('SONAR_TOKEN', env)

    def test_normal_engineering_manifest_receives_requested_scanner_credentials(self):
        runner = AxAgentRunner('image@sha256:' + 'a' * 64, 'test', 'http://model.test/v1')
        role = {'tools':['bash'], 'env_names':['GITGUARDIAN_API_KEY','SONAR_TOKEN']}
        with patch.dict('os.environ', {'GITGUARDIAN_API_KEY':'test-guardian','SONAR_TOKEN':'test-sonar'}):
            env = {item['name']: item['value'] for item in runner.task_manifest('2222222222222222', role)['spec']['env']}
        self.assertEqual(env['GITGUARDIAN_API_KEY'], 'test-guardian')
        self.assertEqual(env['SONAR_TOKEN'], 'test-sonar')

    def test_capabilities_fit_substrate_env_limit_and_are_restored_inside_worker(self):
        role = {**self.roles['backend_engineer'], 'env_names': [
            'GITHUB_PERSONAL_ACCESS_TOKEN','COGNEE_API_URL','COGNEE_PASSWORD',
            'GITGUARDIAN_API_KEY','SONAR_TOKEN']}
        runner = AxAgentRunner('image@sha256:' + 'a' * 64, 'test', 'http://model.test/v1',
                               proxy_token='model-token', model_parameters={'max_tokens':8192})
        runner.delegation = self.service
        runner.harness2_environment = {'HARNESS2_IMPROVER_MODEL':'ax-harness-free',
            'HARNESS2_PROXY_TOKEN':'improver-token','HARNESS2_MODEL_PARAMETERS_JSON':'{}'}
        with patch.dict('os.environ', {name:'test-' + name for name in role['env_names']}):
            manifest = runner.task_manifest('4444444444444444', role)
        env = {item['name']:item['value'] for item in manifest['spec']['env']}
        self.assertLessEqual(len(env), 32)
        from ax_local.worker.entrypoint import load_capabilities
        with patch.dict('os.environ', env, clear=True):
            load_capabilities()
            import os
            self.assertEqual(os.environ['HARNESS2_PROXY_TOKEN'], 'improver-token')
            self.assertIn('AX_HARNESS2_TOKEN', os.environ)
            self.assertIn('AX_DELEGATION_TOKEN', os.environ)
            self.assertNotIn('AX_CAPABILITIES_JSON', os.environ)

    def test_worker_probe_uses_real_a2a_transport_and_returns_evidence(self):
        from ax_local.worker.delegation import run_probe
        server = start_a2a_server(self.service, '127.0.0.1', 0)
        self.addCleanup(server.server_close)
        environment = {**self.environment, 'AX_HARNESS2_URL': self.service.base_url}
        with self.service.turn(self.actor, 5):
            result = run_probe('Fix supplied code', 'Run the regression test.',
                environ=environment, timeout_seconds=3, call_id='native-probe')
        self.assertFalse(result.get('isError'), result)
        self.assertIn('Fix supplied code', result['answer'])
        self.assertIn('sessionId', result)
        self.assertIn('activity', result)
        self.assertTrue(self.runner.chats[-1].closed)

    def test_worker_timeout_cancels_probe_without_consuming_the_normal_delegation_budget(self):
        from ax_local.worker.delegation import run_probe
        def wait(chat, task):
            while not chat.closed:
                time.sleep(.01)
        self.runner.callback = wait
        server = start_a2a_server(self.service, '127.0.0.1', 0)
        self.addCleanup(server.server_close)
        environment = {**self.environment, 'AX_HARNESS2_URL': self.service.base_url}
        with self.service.turn(self.actor, 5):
            result = run_probe('Probe with a deadline', '', environ=environment,
                               timeout_seconds=.3, call_id='timeout-probe')
            self.assertTrue(result['isError'])
        self.assertTrue(self.runner.chats[-1].closed)

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from ax_local.api.delegation import A2AError, DelegationService, start_a2a_server
from ax_local.config import AX_CONFIG
from ax_local.worker.delegation import make_tools
from ax_local.tests import test_delegation as delegation_tests
from popot_agents.orchestrator.main import create_server, load_agents, load_roles
from popot_agents.orchestrator.session_store import SessionStore
from popot_agents.skills import prepare_role_config
from tests import test_chat as chat_tests
from tests import test_roles as role_tests


ROOT = Path(__file__).resolve().parents[1]


def configured_roles():
    roles = load_roles(ROOT / 'config/roles.json', load_agents(ROOT / 'config/agents.json'))
    return {name: {**config, '_ax_role': name} for name, config in roles.items()}


class PlannerRoleTests(unittest.TestCase):
    def test_planner_can_start_a_session_and_continue_with_the_same_role(self):
        roles = configured_roles()
        runner = role_tests.FakeRunner()
        with tempfile.TemporaryDirectory() as directory:
            server = create_server({'omniroute': runner}, roles=roles, port=0, session_dir=directory)
            try:
                status, first = chat_tests.TaskSessionTests._post_to_handler(
                    server.RequestHandlerClass, server,
                    {'role': 'planner', 'message': 'Discuss SPEC-1 before implementation.'})
                self.assertEqual(status, 200, first)
                self.assertEqual(first['role'], 'planner')
                saved = server.store.get(first['sessionId'])
                self.assertEqual(saved['role'], 'planner')
                self.assertIn('write_file', saved['roleConfig']['tools'])
                status, second = chat_tests.TaskSessionTests._post_to_handler(
                    server.RequestHandlerClass, server,
                    {'sessionId': first['sessionId'], 'message': 'Implement the agreed SPEC-1.'})
                self.assertEqual(status, 200, second)
                self.assertEqual(second['role'], 'planner')
                self.assertEqual(len(runner.chats), 1)
                self.assertEqual(len(server.store.get(first['sessionId'])['messages']), 4)
            finally:
                server.server_close()

    def test_planner_has_spec_skills_and_specialists_without_shell_or_github_credentials(self):
        roles = configured_roles()
        planner = roles['planner']
        with redirect_stderr(io.StringIO()):
            prepared = prepare_role_config(planner)
        self.assertIn('engineering/to-spec', planner['skills'])
        self.assertIn('# Selected skill references', prepared['instructions'])
        self.assertNotIn('bash', planner['tools'])
        self.assertNotIn('GITHUB_PERSONAL_ACCESS_TOKEN', planner['env_names'])
        with tempfile.TemporaryDirectory() as directory:
            runner = delegation_tests.FakeRunner()
            service = DelegationService(roles, AX_CONFIG['delegation'], {config['agent']: runner for config in roles.values()},
                                         SessionStore(directory), 'http://127.0.0.1:8005')
            try:
                env = service.worker_environment('0123456789abcdef', planner)
                tool = make_tools(env)['delegate_task']['schema']['function']
                self.assertEqual(set(tool['parameters']['properties']['role']['enum']), {
                    'tech_lead', 'backend_engineer', 'frontend_engineer', 'qa_engineer',
                    'code_reviewer', 'analyst', 'project_operator'})
                self.assertIn(roles['code_reviewer']['description'], tool['description'])
                with service.turn('0123456789abcdef', 5), self.assertRaises(A2AError):
                    service.dispatch(env['AX_DELEGATION_TOKEN'], 'cron_demo', 'SendMessage', {})
            finally:
                service.close()

    def test_planner_hands_specs_and_review_feedback_to_separate_children_and_receives_results(self):
        roles = configured_roles()
        planner = roles['planner']
        received = []
        class ChildChat(delegation_tests.FakeChat):
            def send(self, task, *, timeout_seconds=None):
                with self.runner.delegation.turn(self.session_id, timeout_seconds):
                    payload = json.loads(task)
                    role = self.config['_ax_role']
                    received.append((self.session_id, role, payload))
                    if role == 'code_reviewer':
                        result = {'decision': 'accepted' if payload['revision'] == 2 else 'needs_changes',
                                  'reason': 'AC-1 verified' if payload['revision'] == 2 else 'AC-1 lacks a negative test'}
                    elif role == 'qa_engineer':
                        result = {'criteria': {'AC-1': 'passed'}, 'evidence': 'positive and negative checks'}
                    else:
                        result = {'revision': payload['revision'], 'diff': 'self-contained patch',
                                  'checks': ['positive', 'negative'] if payload['revision'] == 2 else ['positive']}
                    return {'answer': json.dumps(result)}
        class Runner(delegation_tests.FakeRunner):
            def start_chat(self, session_id, config, **_kwargs):
                self.delegation.worker_environment(session_id, config)
                chat = ChildChat(self, session_id, config)
                self.chats.append(chat)
                return chat
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            store = SessionStore(directory)
            runner = Runner()
            service = DelegationService(roles, AX_CONFIG['delegation'], {config['agent']: runner for config in roles.values()}, store, 'http://127.0.0.1:8005')
            runner.delegation = service
            server = start_a2a_server(service, '127.0.0.1', 0)
            parent = '0123456789abcdef'
            env = service.worker_environment(parent, planner)
            env['AX_DELEGATION_URL'] = f'http://127.0.0.1:{server.server_port}'
            env['AX_DELEGATION_POLL_SECONDS'] = '0.01'
            delegate = make_tools(env)['delegate_task']['call']
            spec = {'id': 'SPEC-1', 'version': 1, 'task': 'T-1',
                    'criteria': {'AC-1': 'Handle both valid and invalid input'}}
            calls = []
            def call(role, payload):
                result = json.loads(delegate({'role': role, 'task': json.dumps(payload)},
                                             timeout_seconds=3, call_id=f'planner-{len(calls)}'))
                self.assertEqual(result['state'], 'TASK_STATE_COMPLETED', result)
                self.assertFalse(result['truncated'])
                calls.append(result)
                return json.loads(result['answer'])
            try:
                with service.turn(parent, 15):
                    first = call('backend_engineer', {'spec': spec, 'revision': 1})
                    review = call('code_reviewer', {'spec': spec, **first})
                    self.assertEqual(review['decision'], 'needs_changes')
                    revised = call('backend_engineer', {'spec': spec, 'revision': 2, 'feedback': review})
                    qa = call('qa_engineer', {'spec': spec, **revised})
                    accepted = call('code_reviewer', {'spec': spec, **revised, 'qa': qa})
                    self.assertEqual(accepted['decision'], 'accepted')
                self.assertEqual(len({result['sessionId'] for result in calls}), 5)
                self.assertEqual(received[2][2]['feedback'], review)
                self.assertTrue(all(payload['spec'] == spec for _, _, payload in received))
                self.assertTrue(all(chat.closed for chat in runner.chats))
                for result in calls:
                    saved = store.get(result['sessionId'])
                    self.assertEqual(saved['roleConfig']['_ax_delegation']['parent_session_id'], parent)
                    self.assertEqual(saved['role'], result['role'])
            finally:
                service.close()
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    unittest.main()

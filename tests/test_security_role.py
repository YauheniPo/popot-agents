"""Security specialization stays reachable without acquiring publication rights."""
from pathlib import Path
import tempfile
import threading
import unittest

from popot_agents.orchestrator.main import create_server, load_agents, load_roles
from popot_agents.skills import prepare_role_config
from popot_agents.worker.delegation_policy import delegation_instructions


ROOT = Path(__file__).resolve().parents[1]


class SecurityRoleTests(unittest.TestCase):
    def roles(self):
        return load_roles(ROOT / 'config/roles', load_agents(ROOT / 'config/agents.json'))

    def test_security_role_loads_its_skill_and_only_readonly_repository_access(self):
        role = self.roles()['security_engineer']
        self.assertEqual(role['agent'], 'omniroute_cloud')
        self.assertIn('engineering/security-review', role['skills'])
        prepared = prepare_role_config(role)
        self.assertIn('security-review', prepared['instructions'])
        self.assertTrue({'bash', 'read_file', 'write_file', 'git_clone'} <= set(role['tools']))
        self.assertTrue(role['mcpServers']['github']['url'].endswith('/readonly'))
        self.assertTrue({'get_commit', 'pull_request_read'} <= set(role['mcpServers']['github']['tools']))
        self.assertFalse({'create_pull_request', 'create_or_update_file', 'pull_request_review_write'} &
                         set(role['mcpServers']['github']['tools']))
        self.assertFalse({'GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL'} & set(role['env_names']))
        self.assertTrue({'COGNEE_PASSWORD', 'SONAR_TOKEN', 'GITGUARDIAN_API_KEY'} <= set(role['env_names']))
        self.assertFalse(role.get('harness2', {}).get('enabled', False))

    def test_security_delegation_matches_team_responsibilities(self):
        roles = self.roles()
        for parent in ('planner', 'tech_lead', 'backend_engineer', 'frontend_engineer',
                       'code_reviewer', 'qa_engineer', 'project_operator'):
            self.assertIn('security_engineer', roles[parent]['allowed_roles'], parent)
        self.assertEqual(set(roles['security_engineer']['allowed_roles']),
                         {'backend_engineer', 'qa_engineer', 'code_reviewer', 'project_operator'})
        self.assertNotIn('security_engineer', roles['chat']['allowed_roles'])

    def test_security_review_is_not_automatically_routed_away_to_general_reviewer(self):
        tools = {'delegate_task': {'schema': {'function': {'description': 'Team delegation',
                 'parameters': {'properties': {'role': {'enum': ['code_reviewer', 'qa_engineer']}}}}}}}
        for key in ('name', '_ax_role'):
            prompt = delegation_instructions({key: 'security_engineer'}, tools)
            self.assertNotIn('delegate before performing your own full review', prompt)
            self.assertIn('Own the security assessment', prompt)

    def test_mcp_client_can_select_configured_security_role(self):
        from popot_agents.mcp_server import OrchestratorClient
        from tests.test_roles import FakeRunner
        role = self.roles()['security_engineer']
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as directory:
            server = create_server({'omniroute_cloud': runner}, port=0,
                                   session_dir=directory, roles={'security_engineer': role})
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = OrchestratorClient('http://127.0.0.1:' + str(server.server_port))
                result = client.run_task('Review the supplied security fixture', role='security_engineer')
                self.assertEqual(result['role'], 'security_engineer')
                self.assertEqual(result['agent'], 'omniroute_cloud')
                self.assertEqual(runner.chats[0][1]['name'], 'security_engineer')
                self.assertIn('engineering/security-review', runner.chats[0][1]['skills'])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()

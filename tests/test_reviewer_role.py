import json
import tempfile
import unittest
from pathlib import Path

from popot_agents.orchestrator.main import load_agents, load_roles

ROOT = Path(__file__).resolve().parents[1]


class ReviewerRoleTests(unittest.TestCase):
    def test_reviewer_can_read_and_comment_but_cannot_edit_or_merge(self):
        roles = load_roles(ROOT / 'config/roles.json', load_agents(ROOT / 'config/agents.json'))
        reviewer = roles['code_reviewer']
        self.assertIn('engineering/code-review', reviewer['skills'])
        self.assertIn('engineering/diagnosing-bugs', reviewer['skills'])
        self.assertEqual(reviewer['tools'], [])
        self.assertEqual(reviewer['allowed_roles'], [])
        self.assertEqual(set(reviewer['mcpServers']['github']['tools']),
                         {'get_file_contents', 'get_commit', 'pull_request_read', 'search_repositories',
                          'pull_request_review_write', 'add_comment_to_pending_review'})
        for name in ('tech_lead', 'backend_engineer', 'frontend_engineer'):
            self.assertIn('code_reviewer', roles[name]['allowed_roles'])
        for name in ('backend_engineer', 'frontend_engineer'):
            self.assertIn('code_reviewer', roles[name]['instructions'])
        for config in roles.values():
            self.assertTrue(config['description'].strip())

    def test_description_is_optional_but_validated_when_present(self):
        roles = {'chat': {'agent': 'openrouter', 'instructions': 'Chat.', 'tools': []}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'roles.json'
            agents = load_agents(ROOT / 'config/agents.json')
            path.write_text(json.dumps(roles))
            load_roles(path, agents)
            roles['chat']['description'] = 'General questions.'
            path.write_text(json.dumps(roles))
            load_roles(path, agents)
            for invalid in ('', ' ', 123, 'x' * 1001):
                roles['chat']['description'] = invalid
                path.write_text(json.dumps(roles))
                with self.subTest(value=str(invalid)[:10]), self.assertRaises(ValueError):
                    load_roles(path, agents)

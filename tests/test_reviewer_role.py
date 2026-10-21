import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from popot_agents.orchestrator.main import load_agents, load_roles
from popot_agents.worker.http_harness import run_http

ROOT = Path(__file__).resolve().parents[1]


class ReviewerRoleTests(unittest.TestCase):
    def test_reviewer_can_read_and_comment_but_cannot_edit_or_merge(self):
        roles = load_roles(ROOT / 'config/roles', load_agents(ROOT / 'config/agents.json'))
        reviewer = roles['code_reviewer']
        self.assertIn('method=submit_pending and event=COMMENT', reviewer['instructions'])
        self.assertNotIn('method=submit and', reviewer['instructions'])
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


class ReviewerRuntimeTests(unittest.TestCase):
    def run_review(self, calls):
        requests = []
        replies = iter([{'tool_calls': calls}, {'content': 'Review status verified.'}])
        def respond(call, **kwargs):
            requests.append(json.loads(call.data))
            return io.BytesIO(json.dumps({'choices': [{'message': next(replies)}]}).encode())
        discovered = {}
        for name in ('pull_request_review_write', 'add_comment_to_pending_review'):
            discovered['mcp__github__' + name] = {
                'server': 'github', 'native_name': name,
                'schema': {'type': 'function', 'function': {'name': 'mcp__github__' + name,
                    'description': 'Methods: create, submit_pending, delete_pending, resolve_thread.',
                    'parameters': {'type': 'object', 'properties': {
                        'method': {'type': 'string', 'enum': ['create', 'submit_pending', 'delete_pending',
                                                            'resolve_thread', 'unresolve_thread']},
                        'event': {'type': 'string', 'enum': ['APPROVE', 'REQUEST_CHANGES', 'COMMENT']}}}}}}
        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1', 'HARNESS_MODEL': 'fake'}, clear=True), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=respond), \
             patch('popot_agents.worker.http_harness.mcp_client.discover_tools', return_value=discovered), \
             patch('popot_agents.worker.http_harness.mcp_client.call_tool', return_value='{"ok":true}') as remote, \
             redirect_stderr(io.StringIO()):
            answer = run_http('Review PR', {'_ax_role': 'code_reviewer', 'tools': [],
                                          'mcpServers': {'github': {}}, 'max_tool_rounds': 3})
        self.assertEqual(answer, 'Review status verified.')
        return requests, remote

    @staticmethod
    def call(name, arguments):
        return {'id': name + str(arguments), 'type': 'function', 'function': {
            'name': 'mcp__github__' + name, 'arguments': json.dumps(arguments)}}

    def test_forbidden_review_operations_never_reach_github(self):
        cases = [{'method': method} for method in ('delete_pending', 'resolve_thread', 'unresolve_thread',
                                                  'submit', 'add_comment_to_pending_review')]
        cases += [{'method': 'create', 'event': 'COMMENT'},
                  {'method': []},
                  {'method': 'submit_pending'},
                  {'method': 'submit_pending', 'event': 'APPROVE'},
                  {'method': 'submit_pending', 'event': 'REQUEST_CHANGES'}]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                requests, remote = self.run_review([self.call('pull_request_review_write', arguments)])
                remote.assert_not_called()
                error = json.loads(requests[-1]['messages'][-1]['content'])
                self.assertTrue(error['isError'])
                self.assertIn('submit_pending', error['error'])
                self.assertIn('add_comment_to_pending_review', error['error'])

    def test_review_workflow_uses_separate_comment_tool_and_comment_submission(self):
        arguments = {'owner': 'owner', 'repo': 'repo', 'pullNumber': 80}
        calls = [self.call('pull_request_review_write', {**arguments, 'method': 'create', 'commitID': 'head'}),
                 self.call('add_comment_to_pending_review', {**arguments, 'body': 'Finding', 'path': 'app.py',
                                                           'line': 4, 'side': 'RIGHT', 'subjectType': 'LINE'}),
                 self.call('pull_request_review_write', {**arguments, 'method': 'submit_pending', 'event': 'COMMENT'})]
        requests, remote = self.run_review(calls)
        self.assertEqual(remote.call_count, 3)
        self.assertEqual([call.args[1] for call in remote.call_args_list],
                         ['pull_request_review_write', 'add_comment_to_pending_review', 'pull_request_review_write'])
        schema = requests[0]['tools'][0]['function']['parameters']['properties']
        self.assertEqual(schema['method']['enum'], ['create', 'submit_pending'])
        self.assertEqual(schema['event']['enum'], ['COMMENT'])
        self.assertNotIn('delete_pending', requests[0]['tools'][0]['function'].get('description', ''))

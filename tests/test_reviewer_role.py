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
from popot_agents.worker.mcp_client import MCPToolError

ROOT = Path(__file__).resolve().parents[1]


class ReviewerRoleTests(unittest.TestCase):
    def test_reviewer_can_read_and_comment_but_cannot_edit_or_merge(self):
        roles = load_roles(ROOT / 'config/roles', load_agents(ROOT / 'config/agents.json'))
        reviewer = roles['code_reviewer']
        self.assertIn('method=submit_pending and event=COMMENT', reviewer['instructions'])
        self.assertNotIn('method=submit and', reviewer['instructions'])
        self.assertIn('engineering/code-review', reviewer['skills'])
        self.assertIn('engineering/diagnosing-bugs', reviewer['skills'])
        self.assertTrue({'bash', 'read_file', 'write_file', 'git_clone'} <= set(reviewer['tools']))
        self.assertEqual(reviewer['allowed_roles'], ['qa_engineer', 'analyst'])
        self.assertEqual(set(reviewer['mcpServers']['github']['tools']),
                         {'get_file_contents', 'get_commit', 'pull_request_read', 'search_repositories',
                          'pull_request_review_write', 'add_comment_to_pending_review'})
        for name in ('tech_lead', 'backend_engineer', 'frontend_engineer'):
            self.assertIn('code_reviewer', roles[name]['allowed_roles'])
        for name in ('backend_engineer', 'frontend_engineer'):
            self.assertIn('code_reviewer', roles[name]['instructions'])
        for config in roles.values():
            self.assertTrue(config['description'].strip())

    def test_review_scope_has_stopping_rule_and_explicit_thread_authorization(self):
        roles = load_roles(ROOT / 'config/roles', load_agents(ROOT / 'config/agents.json'))
        instructions = roles['code_reviewer']['instructions']
        self.assertIn('Do not resolve or reopen threads unless the original user explicitly requests it', instructions)
        self.assertIn('Do not reconsider a rejected candidate without new evidence', instructions)
        self.assertIn('no new findings is a completed review', instructions)
        self.assertNotIn('resolve or reopen threads based on verified code', instructions)

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
    def run_review(self, calls, *, remote_effect=None, separate_rounds=False, state=None):
        requests = []
        messages = ([{'tool_calls': [call]} for call in calls] if separate_rounds else [{'tool_calls': calls}])
        final = {'status': 'completed', 'answer': 'Review status verified.',
                 'evidence': ['verified tool response'], 'remaining_work': [], 'errors': []}
        replies = iter([*messages, {'content': json.dumps(final) if state is not None else final['answer']}])
        def respond(call, **kwargs):
            body = json.loads(call.data)
            requests.append(body)
            message = next(replies)
            return io.BytesIO(json.dumps({'choices': [{'message': {'role': 'assistant', **message}}]}).encode())
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
             patch('popot_agents.worker.http_harness.mcp_client.call_tool', return_value='{"ok":true}',
                   side_effect=remote_effect) as remote, \
             redirect_stderr(io.StringIO()):
            answer = run_http('Review PR', {'_ax_role': 'code_reviewer', 'tools': [],
                                          'mcpServers': {'github': {}}, 'max_tool_rounds': len(messages) + 2}, state=state)
        self.assertEqual(answer, 'Review status verified.')
        return requests, remote

    @staticmethod
    def call(name, arguments):
        return {'id': name + str(arguments), 'type': 'function', 'function': {
            'name': 'mcp__github__' + name, 'arguments': json.dumps(arguments)}}

    def test_review_workflow_uses_separate_comment_tool_and_comment_submission(self):
        arguments = {'owner': 'owner', 'repo': 'repo', 'pullNumber': 80}
        calls = [self.call('pull_request_review_write', {**arguments, 'method': 'create', 'commitID': 'head'}),
                 self.call('add_comment_to_pending_review', {**arguments, 'body': 'Finding', 'path': 'app.py',
                                                           'line': 4, 'side': 'RIGHT', 'subjectType': 'LINE'}),
                 self.call('pull_request_review_write', {**arguments, 'method': 'submit_pending', 'event': 'COMMENT'})]
        requests, remote = self.run_review(calls, separate_rounds=True)
        self.assertEqual(remote.call_count, 3)
        self.assertEqual([call.args[1] for call in remote.call_args_list],
                         ['pull_request_review_write', 'add_comment_to_pending_review', 'pull_request_review_write'])
        schema = requests[0]['tools'][0]['function']['parameters']['properties']
        self.assertIn('delete_pending', schema['method']['enum'])
        self.assertEqual(schema['event']['enum'], ['APPROVE', 'REQUEST_CHANGES', 'COMMENT'])
        self.assertIn('delete_pending', requests[0]['tools'][0]['function']['description'])

    def test_other_create_errors_can_be_corrected_without_a_publication_block(self):
        target = {'owner': 'owner', 'repo': 'repo', 'pullNumber': 80}
        calls = [self.call('pull_request_review_write', {**target, 'method': 'create', 'commitID': commit})
                 for commit in ('bad-ref', 'head')]
        calls.append(self.call('pull_request_review_write', {**target, 'method': 'submit_pending', 'event': 'COMMENT'}))
        _, remote = self.run_review(calls, separate_rounds=True,
            remote_effect=[MCPToolError('Commit could not be resolved'), '{"ok":true}', '{"ok":true}'])
        self.assertEqual(remote.call_count, 3)

    def test_inline_comment_requires_diff_line_and_side_before_github_call(self):
        target = {'owner': 'owner', 'repo': 'repo', 'pullNumber': 80}
        for position in ({}, {'line': 4}, {'line': 0, 'side': 'RIGHT'}, {'line': 4, 'side': 'MIDDLE'}):
            with self.subTest(position=position):
                requests, remote = self.run_review([
                    self.call('pull_request_review_write', {**target, 'method': 'create', 'commitID': 'head'}),
                    self.call('add_comment_to_pending_review', {**target, 'body': 'Finding', 'path': 'app.py',
                                                               'subjectType': 'LINE', **position})])
                self.assertEqual(remote.call_count, 1)
                self.assertTrue(json.loads(requests[-1]['messages'][-1]['content'])['isError'])

    def test_existing_draft_can_be_submitted_or_deleted(self):
        target = {'owner': 'owner', 'repo': 'repo', 'pullNumber': 80}
        for operation in ({'method': 'delete_pending'},
                          {'method': 'submit_pending', 'event': 'COMMENT'},
                          {'method': 'submit_pending', 'event': 'APPROVE'},
                          {'method': 'submit_pending', 'event': 'REQUEST_CHANGES'}):
            with self.subTest(operation=operation):
                _, remote = self.run_review([self.call('pull_request_review_write', {**target, **operation})])
                remote.assert_called_once()
                self.assertEqual(remote.call_args.args[2], {**target, **operation})

    def test_pending_conflict_can_be_recovered_by_deleting_and_recreating(self):
        target = {'owner': 'owner', 'repo': 'repo', 'pullNumber': 80}
        operations = [{'method': 'create', 'commitID': 'head'}, {'method': 'delete_pending'},
                      {'method': 'create', 'commitID': 'head'},
                      {'method': 'submit_pending', 'event': 'COMMENT'}]
        requests, remote = self.run_review(
            [self.call('pull_request_review_write', {**target, **operation}) for operation in operations],
            separate_rounds=True,
            remote_effect=[MCPToolError('User can only have one pending review per pull request'),
                           '{"ok":true}', '{"ok":true}', '{"ok":true}'])
        self.assertEqual(remote.call_count, 4)
        self.assertTrue(all('tools' in body for body in requests))

    def test_existing_draft_accepts_inline_comments(self):
        target = {'owner': 'owner', 'repo': 'repo', 'pullNumber': 80}
        _, remote = self.run_review([self.call('add_comment_to_pending_review',
            {**target, 'body': 'Finding', 'path': 'app.py', 'line': 4, 'side': 'RIGHT', 'subjectType': 'LINE'})])
        remote.assert_called_once()

    def test_old_conflict_checkpoint_does_not_disable_review_recovery(self):
        target = {'owner': 'owner', 'repo': 'repo', 'pullNumber': 80}
        state = {'messages': [{'role': 'user', 'content': 'Review PR'}],
                 'review_conflict': 'https://github.com/owner/repo/pull/80',
                 'outcome': {'status': 'blocked'}}
        _, remote = self.run_review([self.call('pull_request_review_write',
            {**target, 'method': 'submit_pending', 'event': 'COMMENT'})], state=state)
        remote.assert_called_once()
        self.assertEqual(state['outcome']['status'], 'completed')
        self.assertNotIn('review_conflict', state)

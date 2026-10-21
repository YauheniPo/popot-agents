import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

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
    def run_review(self, calls, *, remote_effect=None, separate_rounds=False, partial=False,
                   final_message=None, final_error=None, read_tools=False):
        requests = []
        messages = ([{'tool_calls': [call]} for call in calls] if separate_rounds else [{'tool_calls': calls}])
        replies = iter([*messages, {'content': 'Review status verified.'}])
        def respond(call, **kwargs):
            body = json.loads(call.data)
            requests.append(body)
            if body.get('tool_choice') == 'none':
                if final_error:
                    raise final_error
                message = final_message or {'content': 'Existing draft blocks publication.'}
            else:
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
        if read_tools:
            for name in ('pull_request_read', 'get_file_contents'):
                discovered['mcp__github__' + name] = {
                    'server': 'github', 'native_name': name,
                    'schema': {'type': 'function', 'function': {'name': 'mcp__github__' + name,
                        'parameters': {'type': 'object'}}}}
        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1', 'HARNESS_MODEL': 'fake'}, clear=True), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=respond), \
             patch('popot_agents.worker.http_harness.mcp_client.discover_tools', return_value=discovered), \
             patch('popot_agents.worker.http_harness.mcp_client.call_tool', return_value='{"ok":true}',
                   side_effect=remote_effect) as remote, \
             redirect_stderr(io.StringIO()):
            answer = run_http('Review PR', {'_ax_role': 'code_reviewer', 'tools': [],
                                          'mcpServers': {'github': {}}, 'max_tool_rounds': len(messages) + 2})
        if partial:
            self.assertIn('Partial result', answer)
            self.assertIn('https://github.com/owner/repo/pull/80', answer)
            self.assertNotIn('work budget exhausted', answer)
        else:
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
        requests, remote = self.run_review(calls, separate_rounds=True)
        self.assertEqual(remote.call_count, 3)
        self.assertEqual([call.args[1] for call in remote.call_args_list],
                         ['pull_request_review_write', 'add_comment_to_pending_review', 'pull_request_review_write'])
        schema = requests[0]['tools'][0]['function']['parameters']['properties']
        self.assertEqual(schema['method']['enum'], ['create', 'submit_pending'])
        self.assertEqual(schema['event']['enum'], ['COMMENT'])
        self.assertNotIn('delete_pending', requests[0]['tools'][0]['function'].get('description', ''))

    def test_existing_pending_review_cannot_be_modified_without_creation_in_this_call(self):
        target = {'owner': 'owner', 'repo': 'repo', 'pullNumber': 80}
        calls = [self.call('add_comment_to_pending_review', {**target, 'body': 'Finding', 'path': 'app.py',
                    'line': 4, 'side': 'RIGHT', 'subjectType': 'LINE'}),
                 self.call('pull_request_review_write', {**target, 'method': 'submit_pending', 'event': 'COMMENT'})]
        requests, remote = self.run_review(calls)
        remote.assert_not_called()
        for item in requests[-1]['messages'][-2:]:
            error = json.loads(item['content'])
            self.assertTrue(error['isError'])
            self.assertIn('this call', error['error'])

    def test_failed_create_does_not_grant_access_to_an_existing_pending_review(self):
        target = {'owner': 'owner', 'repo': 'repo', 'pullNumber': 80}
        calls = [self.call('pull_request_review_write', {**target, 'method': 'create', 'commitID': 'head'}),
                 self.call('add_comment_to_pending_review', {**target, 'body': 'Finding', 'path': 'app.py',
                    'line': 4, 'side': 'RIGHT', 'subjectType': 'LINE'}),
                 self.call('pull_request_review_write', {**target, 'method': 'submit_pending', 'event': 'COMMENT'})]
        requests, remote = self.run_review(calls, remote_effect=[
            MCPToolError('User can only have one pending review per pull request'), '{"ok":true}', '{"ok":true}'],
            partial=True)
        self.assertEqual(remote.call_count, 1)
        errors = [json.loads(item['content']) for item in requests[-1]['messages'] if item['role'] == 'tool']
        self.assertEqual(len(errors), 3)
        self.assertTrue(all(item['isError'] for item in errors))

    def test_pending_conflict_forces_a_status_answer_when_no_read_tools_remain(self):
        target = {'owner': 'owner', 'repo': 'repo', 'pullNumber': 80}
        create = self.call('pull_request_review_write', {**target, 'method': 'create', 'commitID': 'head'})
        requests, remote = self.run_review([create, create, create], separate_rounds=True,
            remote_effect=MCPToolError('MCPToolError: User can only have one pending review per pull request'),
            partial=True)
        remote.assert_called_once()
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[-1]['tool_choice'], 'none')
        self.assertNotIn('tools', requests[-1])
        self.assertIn('pending review', requests[-1]['messages'][-1]['content'])
        self.assertNotIn('budget is exhausted', requests[-1]['messages'][-1]['content'])
        error = next(item for item in requests[-1]['messages'] if item['role'] == 'tool')
        self.assertIn('Do not retry', json.loads(error['content'])['next_step'])

    def test_pending_conflict_fallback_preserves_the_reason_when_final_model_cannot_answer(self):
        target = {'owner': 'owner', 'repo': 'repo', 'pullNumber': 80}
        create = self.call('pull_request_review_write', {**target, 'method': 'create', 'commitID': 'head'})
        for response in ({'final_error': TimeoutError()}, {'final_message': {'tool_calls': [create]}}):
            with self.subTest(response=response):
                requests, remote = self.run_review([create],
                    remote_effect=MCPToolError('User can only have one pending review per pull request'),
                    partial=True, **response)
                remote.assert_called_once()
                self.assertEqual(len(requests), 2)

    def test_pending_conflict_leaves_reads_available_to_finish_the_review(self):
        target = {'owner': 'owner', 'repo': 'repo', 'pullNumber': 80}
        calls = [self.call('pull_request_review_write', {**target, 'method': 'create', 'commitID': 'head'}),
                 self.call('pull_request_read', {**target, 'method': 'get_diff'}),
                 self.call('get_file_contents', {'owner': 'owner', 'repo': 'repo', 'path': 'app.py', 'ref': 'head'})]
        memory = Mock(enabled=True, dataset='repo-memory', run_complete=True)
        memory.context.return_value = ''
        with patch('popot_agents.worker.http_harness.TurnMemory', return_value=memory):
            requests, remote = self.run_review(calls, separate_rounds=True, read_tools=True, partial=True,
                remote_effect=[MCPToolError('User can only have one pending review per pull request'),
                               'diff evidence', 'source evidence'])
        self.assertEqual(remote.call_count, 3)
        self.assertEqual(len(requests), 4)
        for request in requests[1:]:
            self.assertEqual({tool['function']['name'] for tool in request['tools']},
                             {'mcp__github__pull_request_read', 'mcp__github__get_file_contents'})
        error = next(item for item in requests[1]['messages'] if item['role'] == 'tool')
        self.assertIn('Continue', json.loads(error['content'])['next_step'])
        memory.finish_session.assert_called_once_with(False)
        memory.summary_messages.assert_not_called()
        memory.save.assert_not_called()

    def test_ignoring_the_publication_block_cannot_restart_the_create_loop(self):
        target = {'owner': 'owner', 'repo': 'repo', 'pullNumber': 80}
        create = self.call('pull_request_review_write', {**target, 'method': 'create', 'commitID': 'head'})
        requests, remote = self.run_review([create, create, create], separate_rounds=True,
            read_tools=True, partial=True,
            remote_effect=MCPToolError('User can only have one pending review per pull request'))
        remote.assert_called_once()
        self.assertEqual(len(requests), 3)
        self.assertIn('tools', requests[1])
        self.assertEqual(requests[2]['tool_choice'], 'none')

    def test_other_create_errors_can_be_corrected_without_a_publication_block(self):
        target = {'owner': 'owner', 'repo': 'repo', 'pullNumber': 80}
        calls = [self.call('pull_request_review_write', {**target, 'method': 'create', 'commitID': commit})
                 for commit in ('bad-ref', 'head')]
        calls.append(self.call('pull_request_review_write', {**target, 'method': 'submit_pending', 'event': 'COMMENT'}))
        _, remote = self.run_review(calls, separate_rounds=True,
            remote_effect=[MCPToolError('Commit could not be resolved'), '{"ok":true}', '{"ok":true}'])
        self.assertEqual(remote.call_count, 3)

    def test_review_ownership_is_scoped_to_target_and_current_call(self):
        target = {'owner': 'owner', 'repo': 'repo', 'pullNumber': 80}
        create = self.call('pull_request_review_write', {**target, 'method': 'create', 'commitID': 'head'})
        comment = self.call('add_comment_to_pending_review', {**target, 'body': 'Finding', 'path': 'app.py',
                           'line': 4, 'side': 'RIGHT', 'subjectType': 'LINE'})
        _, remote = self.run_review([create, self.call('add_comment_to_pending_review', {
            **target, 'pullNumber': 81, 'body': 'Other PR', 'path': 'app.py',
            'line': 4, 'side': 'RIGHT', 'subjectType': 'LINE'})])
        self.assertEqual(remote.call_count, 1)
        _, remote = self.run_review([comment])
        remote.assert_not_called()

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

    def test_submitted_review_cannot_be_recreated_or_modified_in_the_same_call(self):
        target = {'owner': 'owner', 'repo': 'repo', 'pullNumber': 80}
        create = self.call('pull_request_review_write', {**target, 'method': 'create', 'commitID': 'head'})
        submit = self.call('pull_request_review_write', {**target, 'method': 'submit_pending', 'event': 'COMMENT'})
        _, remote = self.run_review([create, create, submit, create, submit])
        self.assertEqual(remote.call_count, 2)

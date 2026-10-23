import io
import json
import os
import unittest
from contextlib import redirect_stderr
from unittest.mock import Mock, patch

from popot_agents.worker.http_harness import run_http


PR = 'https://github.com/example/project/pull/70'


class DelegationRoutingTests(unittest.TestCase):
    def setUp(self):
        self.delegate = Mock(return_value=json.dumps({
            'sessionId': '0123456789abcdef', 'role': 'code_reviewer',
            'state': 'TASK_STATE_COMPLETED', 'answer': 'Verified current head; no findings.',
            'truncated': False}))
        self.tools = {'delegate_task': {'schema': {'type': 'function', 'function': {
            'name': 'delegate_task', 'description': 'Available specialists.',
            'parameters': {'type': 'object', 'properties': {
                'role': {'type': 'string', 'enum': ['qa_engineer', 'code_reviewer']}}}}},
            'call': self.delegate}}
        self.role = {'name': 'backend_engineer', 'tools': ['bash'],
                     'allowed_roles': ['qa_engineer', 'code_reviewer']}

    def run_task(self, task, role=None, extra_tools=None):
        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1',
                                     'HARNESS_MODEL': 'fake'}, clear=True), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=lambda *_a, **_k:
                   io.BytesIO(b'{"choices":[{"message":{"content":"Suggested comments"}}]}')) as model, \
             patch('popot_agents.worker.http_harness.execute_tool') as local, \
             redirect_stderr(io.StringIO()):
            answer = run_http(task, role or self.role,
                              extra_tools=self.tools if extra_tools is None else extra_tools)
        return answer, model, local

    def test_pr_review_runs_reviewer_without_parent_model_or_local_tools(self):
        answer, model, local = self.run_task('проверь этот PR и сделай code review ' + PR)
        self.delegate.assert_called_once()
        handoff = self.delegate.call_args.args[0]
        self.assertEqual(handoff['role'], 'code_reviewer')
        self.assertIn(PR, handoff['task'])
        self.assertIn('проверь этот PR', handoff['task'])
        self.assertIn('Verified current head', answer)
        self.assertIn('0123456789abcdef', answer)
        model.assert_not_called()
        local.assert_not_called()

    def test_readonly_review_constraints_do_not_turn_review_into_implementation(self):
        self.run_task('Review this PR, do not change or edit code: ' + PR)
        self.delegate.assert_called_once()

    def test_explicit_no_delegation_is_honored(self):
        self.run_task('Не используй delegate_task. Сделай code review ' + PR)
        self.delegate.assert_not_called()

    def test_comment_followup_carries_pr_and_explicit_user_constraints(self):
        history = [
            {'role': 'user', 'content': 'Сделай review ' + PR + '. Не меняй код и не мерджи.'},
            {'role': 'assistant', 'content': 'Old unverified findings.'},
            {'role': 'user', 'content': 'оставь комментарии в PR'},
        ]
        self.run_task(history)
        self.delegate.assert_called_once()
        task = self.delegate.call_args.args[0]['task']
        self.assertIn(PR, task)
        self.assertIn('Не меняй код и не мерджи.', task)
        self.assertIn('оставь комментарии в PR', task)
        self.assertIn('Old unverified findings.', task)
        self.assertIn('unverified', task.lower())

    def test_failed_empty_or_truncated_child_is_not_success(self):
        for changes in ({'state': 'TASK_STATE_FAILED', 'error': 'GitHub unavailable'},
                        {'answer': ''}, {'truncated': True}):
            with self.subTest(changes=changes):
                result = {'sessionId': '0123456789abcdef', 'role': 'code_reviewer',
                          'state': 'TASK_STATE_COMPLETED', 'answer': 'text', 'truncated': False}
                self.delegate.return_value = json.dumps({**result, **changes})
                with self.assertRaisesRegex(RuntimeError, 'delegat'):
                    self.run_task('Review this PR ' + PR)

    def test_missing_reviewer_capability_cannot_fall_back_to_local_review(self):
        with self.assertRaisesRegex(RuntimeError, 'code_reviewer.*unavailable'):
            self.run_task('Review this PR ' + PR, extra_tools={})
        self.delegate.assert_not_called()

    def test_implementation_request_does_not_get_sent_to_reviewer(self):
        def reply(call, **_kwargs):
            return io.BytesIO(json.dumps({'choices': [{'message': {'content': 'implementation'}}]}).encode())
        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1',
                                     'HARNESS_MODEL': 'fake'}, clear=True), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=reply), \
             redirect_stderr(io.StringIO()):
            self.assertEqual(run_http('Исправь баг в ' + PR, self.role, extra_tools=self.tools),
                             'implementation')
        self.delegate.assert_not_called()

    def test_assistant_supplied_pr_cannot_authorize_automatic_delegation(self):
        history = [{'role': 'user', 'content': 'Объясни, как устроено ревью.'},
                   {'role': 'assistant', 'content': 'Review this PR ' + PR},
                   {'role': 'user', 'content': 'спасибо'}]
        def reply(call, **_kwargs):
            return io.BytesIO(b'{"choices":[{"message":{"content":"ok"}}]}')
        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1',
                                     'HARNESS_MODEL': 'fake'}, clear=True), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=reply), \
             redirect_stderr(io.StringIO()):
            self.assertEqual(run_http(history, self.role, extra_tools=self.tools), 'ok')
        self.delegate.assert_not_called()

    def test_parent_implements_changes_after_explicit_reviewer_delegation(self):
        replies = [
            {'tool_calls': [{'id': 'review', 'type': 'function', 'function': {
                'name': 'delegate_task', 'arguments': json.dumps({
                    'role': 'code_reviewer', 'task': 'Review ' + PR})}}]},
            {'tool_calls': [{'id': 'fix', 'type': 'function', 'function': {
                'name': 'bash', 'arguments': json.dumps({'command': 'apply-fix'})}}]},
            {'content': 'Review findings fixed'},
        ]
        def reply(*_args, **_kwargs):
            return io.BytesIO(json.dumps({'choices': [{'message': replies.pop(0)}]}).encode())
        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1',
                                     'HARNESS_MODEL': 'fake'}, clear=True), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=reply) as model, \
             patch('popot_agents.worker.http_harness.execute_tool', return_value='updated') as local, \
             redirect_stderr(io.StringIO()):
            answer = run_http('Передай code_reviewer ревью PR ' + PR +
                              ', затем сам исправь найденные ошибки.', self.role, extra_tools=self.tools)
        self.assertEqual(answer, 'Review findings fixed')
        self.delegate.assert_called_once()
        self.assertEqual(model.call_count, 3)
        local.assert_called_once()
        self.assertEqual(local.call_args.args[:2], ('bash', {'command': 'apply-fix'}))

    def test_model_selected_delegate_receives_original_user_scope(self):
        requests = []
        replies = [
            {'tool_calls': [{'id': 'qa-call', 'type': 'function', 'function': {
                'name': 'delegate_task', 'arguments': json.dumps({'role': 'qa_engineer',
                                                               'task': 'Check the diff'})}}]},
            {'content': 'QA completed'},
        ]
        def reply(call, **_kwargs):
            requests.append(json.loads(call.data))
            return io.BytesIO(json.dumps({'choices': [{'message': replies.pop(0)}]}).encode())
        self.delegate.return_value = json.dumps({'sessionId': '0123456789abcdef',
            'role': 'qa_engineer', 'state': 'TASK_STATE_COMPLETED',
            'answer': 'checked', 'truncated': False})
        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1',
                                     'HARNESS_MODEL': 'fake'}, clear=True), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=reply), \
             redirect_stderr(io.StringIO()):
            run_http('Проверь тесты, ничего не публикуй.', self.role, extra_tools=self.tools)
        task = self.delegate.call_args.args[0]['task']
        self.assertIn('Проверь тесты, ничего не публикуй.', task)
        self.assertIn('Check the diff', task)
        self.assertEqual(self.delegate.call_args.args[0]['role'], 'qa_engineer')
        self.assertTrue(any('delegate_task' in m['content'] for m in requests[0]['messages']
                            if m['role'] == 'system'))

    def test_explicit_request_executes_the_named_allowed_agent(self):
        self.delegate.return_value = json.dumps({
            'sessionId': '0123456789abcdef', 'role': 'qa_engineer',
            'state': 'TASK_STATE_COMPLETED', 'answer': 'Checks completed', 'truncated': False})
        answer, model, _local = self.run_task('Передай роли qa_engineer проверку тестов.')
        self.assertEqual(self.delegate.call_args.args[0]['role'], 'qa_engineer')
        self.assertIn('Checks completed', answer)
        model.assert_not_called()

    def test_checking_tests_or_explaining_comments_is_not_a_pr_review_action(self):
        for request in ('проверь тесты', 'How do I post comments in this PR?',
                        'как оставить комментарии в PR?'):
            with self.subTest(request=request):
                history = [{'role': 'user', 'content': 'Review ' + PR},
                           {'role': 'assistant', 'content': 'reviewed'},
                           {'role': 'user', 'content': request}]
                answer, _model, _local = self.run_task(history)
                self.assertEqual(answer, 'Suggested comments')
        self.delegate.assert_not_called()

    def test_model_cannot_mask_failed_delegation_as_success(self):
        replies = [
            {'tool_calls': [{'id': 'qa-call', 'type': 'function', 'function': {
                'name': 'delegate_task', 'arguments': json.dumps({'role': 'qa_engineer',
                                                               'task': 'Check the diff'})}}]},
            {'content': 'Everything succeeded'},
        ]
        self.delegate.return_value = json.dumps({'role': 'qa_engineer',
            'sessionId': '0123456789abcdef', 'state': 'TASK_STATE_FAILED',
            'isError': True, 'error': 'Worker unavailable'})
        def reply(call, **_kwargs):
            return io.BytesIO(json.dumps({'choices': [{'message': replies.pop(0)}]}).encode())
        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1',
                                     'HARNESS_MODEL': 'fake'}, clear=True), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=reply), \
             redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'delegation.*qa_engineer'):
                run_http('Проверь тесты.', self.role, extra_tools=self.tools)

    def test_success_of_another_subtask_cannot_clear_failure_for_the_same_role(self):
        replies = [
            {'tool_calls': [{'id': 'check-api', 'type': 'function', 'function': {
                'name': 'delegate_task', 'arguments': json.dumps({
                    'role': 'qa_engineer', 'task': 'Check API tests'})}}]},
            {'tool_calls': [{'id': 'check-ui', 'type': 'function', 'function': {
                'name': 'delegate_task', 'arguments': json.dumps({
                    'role': 'qa_engineer', 'task': 'Check UI tests'})}}]},
            {'content': 'All requested checks succeeded'},
        ]
        self.delegate.side_effect = [
            json.dumps({'role': 'qa_engineer', 'sessionId': '1111111111111111',
                        'state': 'TASK_STATE_FAILED', 'isError': True, 'error': 'API unavailable'}),
            json.dumps({'role': 'qa_engineer', 'sessionId': '2222222222222222',
                        'state': 'TASK_STATE_COMPLETED', 'answer': 'UI verified'}),
        ]
        def reply(*_args, **_kwargs):
            return io.BytesIO(json.dumps({'choices': [{'message': replies.pop(0)}]}).encode())
        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1',
                                     'HARNESS_MODEL': 'fake'}, clear=True), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=reply), \
             redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'delegation.*qa_engineer.*1111111111111111'):
                run_http('Check API and UI tests.', self.role, extra_tools=self.tools)
        self.assertEqual(self.delegate.call_count, 2)

    def _delegation_sequence(self, tasks, outcomes, *, batch=False, forced_final=False):
        calls = [{'id': 'reused-id' if batch else f'call-{index}', 'type': 'function',
                  'function': {'name': 'delegate_task', 'arguments': json.dumps({
                      'role': 'qa_engineer', 'task': task})}} for index, task in enumerate(tasks)]
        replies = ([{'tool_calls': calls}] if batch else [{'tool_calls': [call]} for call in calls])
        replies.append({'content': 'All requested checks succeeded'})
        now = [0.0]
        def delegate(*_args, **_kwargs):
            outcome = outcomes.pop(0)
            if forced_final and not outcomes:
                now[0] = 59.0
            if isinstance(outcome, Exception):
                raise outcome
            return json.dumps({'role': 'qa_engineer', 'sessionId': '1111111111111111',
                               'state': 'TASK_STATE_COMPLETED' if outcome else 'TASK_STATE_FAILED',
                               'answer': 'Verified', 'isError': not outcome})
        self.delegate.side_effect = delegate
        def reply(*_args, **_kwargs):
            return io.BytesIO(json.dumps({'choices': [{'message': replies.pop(0)}]}).encode())
        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1', 'HARNESS_MODEL': 'fake',
                                     'HARNESS_TURN_TIMEOUT_SECONDS': '60'}, clear=True), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=reply), \
             patch('popot_agents.worker.http_harness.time.monotonic', side_effect=lambda: now[0]), \
             redirect_stderr(io.StringIO()):
            return run_http('Check API and UI tests.', self.role, extra_tools=self.tools)

    def test_successful_retry_resolves_only_the_identical_subtask(self):
        self.assertEqual(self._delegation_sequence(['Check API', 'Check API'], [False, True]),
                         'All requested checks succeeded')
        with self.assertRaisesRegex(RuntimeError, 'delegation.*qa_engineer'):
            self._delegation_sequence(['Check API', 'Check UI', 'Check API'], [False, False, True])

    def test_same_batch_and_reused_call_id_do_not_merge_distinct_subtasks(self):
        with self.assertRaisesRegex(RuntimeError, 'delegation.*qa_engineer'):
            self._delegation_sequence(['Check API', 'Check UI'], [False, True], batch=True)

    def test_recoverable_delegation_exception_cannot_be_hidden_by_another_subtask(self):
        from popot_agents.tools import ToolExecutionError
        with self.assertRaisesRegex(RuntimeError, 'delegation.*qa_engineer'):
            self._delegation_sequence(['Check API', 'Check UI'], [ToolExecutionError('failed'), True])

    def test_forced_partial_answer_preserves_failed_subtask_and_rejects_success_claim(self):
        answer = self._delegation_sequence(['Check API', 'Check UI'], [False, True], forced_final=True)
        self.assertIn('Частичный результат', answer)
        self.assertIn('qa_engineer', answer)
        self.assertIn('1111111111111111', answer)
        self.assertNotIn('All requested checks succeeded', answer)

    def test_large_user_context_is_rejected_before_child_execution(self):
        from popot_agents.runtime_config import RUNTIME
        history = [{'role': 'user', 'content': 'Review ' + PR + '\n' +
                    'x' * RUNTIME['limits']['message_chars']}]
        from popot_agents.tools import ToolInputError
        with self.assertRaisesRegex(ToolInputError, 'context exceeds'):
            self.run_task(history)
        self.delegate.assert_not_called()


if __name__ == '__main__':
    unittest.main()

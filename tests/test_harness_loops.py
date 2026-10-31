import io
import json
import os
import subprocess
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from popot_agents import tools
from popot_agents.runtime_config import RUNTIME
from popot_agents.progress import observe_progress
from popot_agents.worker.agent_worker import run_harness
from popot_agents.worker.http_harness import run_http


def tool_call(identifier, name, arguments):
    return {'id': identifier, 'type': 'function', 'function': {
        'name': name, 'arguments': arguments if isinstance(arguments, str) else json.dumps(arguments)}}


class HttpRecoveryLoopTests(unittest.TestCase):
    def test_final_report_reads_tool_history_as_evidence_without_continuing_the_tool_loop(self):
        requests, state = [], {}
        report = {'status': 'completed', 'answer': 'Verified calculation; publication remains.',
                  'evidence': ['calculation returned 4'], 'remaining_work': ['publish'], 'errors': []}
        def respond(call, **kwargs):
            body = json.loads(call.data)
            requests.append(body)
            if len(requests) == 1:
                message = {'tool_calls': [tool_call('proof', 'calculate', {'expression': '2+2'})]}
            elif any(item['role'] == 'tool' for item in body['messages']):
                # Reproduce the provider returning its report through bash echo.
                message = {'tool_calls': [tool_call('echo-report', 'bash', {'command': 'echo report'})]}
            else:
                message = {'content': json.dumps(report)}
            return io.BytesIO(json.dumps({'choices': [{'message': message}]}).encode())
        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1', 'HARNESS_MODEL': 'fake'}, clear=True), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=respond), \
             patch('popot_agents.worker.http_harness.execute_tool', return_value='4') as execute, \
             redirect_stderr(io.StringIO()):
            answer = run_http('Calculate and publish', {'tools': ['calculate', 'bash'], 'max_tool_rounds': 1},
                              state=state)
        self.assertIn('Verified calculation', answer)
        self.assertEqual(state['outcome']['status'], 'partial')
        self.assertEqual(state['outcome']['evidence'], ['calculation returned 4'])
        self.assertEqual(len(requests), 2)
        execute.assert_called_once()
        transcript = json.loads(requests[-1]['messages'][-1]['content'])
        proof = {'role': 'tool', 'tool_call_id': 'proof', 'content': '4'}
        self.assertIn({'source': 'tool_result', 'tool': 'calculate',
                       'call_id': 'proof', 'content': '4'}, transcript)
        self.assertIn(proof, state['messages'])
        self.assertNotIn('tools', requests[-1])
        self.assertEqual(requests[-1]['tool_choice'], 'none')

    def test_structured_status_is_language_independent_and_unfinished_work_cannot_be_complete(self):
        for text in ('Completed', 'Готово', '完了'):
            with self.subTest(text=text):
                state = {}
                response = {'choices': [{'message': {'content': json.dumps({
                    'status': 'completed', 'answer': text, 'evidence': ['diff read'],
                    'remaining_work': ['verify tests'], 'errors': []})}}]}
                with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1', 'HARNESS_MODEL': 'fake'}, clear=True), \
                     patch('popot_agents.worker.http_harness.request.urlopen',
                           return_value=io.BytesIO(json.dumps(response).encode())), \
                     redirect_stderr(io.StringIO()):
                    self.assertEqual(run_http('review', {}, state=state), text)
                self.assertEqual(state['outcome']['status'], 'partial')
                self.assertEqual(state['outcome']['remaining_work'], ['verify tests'])

    def test_partial_checkpoint_restores_tool_evidence_without_replaying_tools(self):
        checkpoint, requests = {}, []
        replies = [
            {'tool_calls': [tool_call('proof', 'calculate', {'expression': '2+2'})]},
            {'content': json.dumps({'status': 'partial', 'answer': 'Still need to verify the result.',
                                   'evidence': ['calculation returned 4'], 'remaining_work': ['verify'], 'errors': []})},
            {'content': json.dumps({'status': 'completed', 'answer': 'Verified the earlier evidence: 4.',
                                   'evidence': ['verified 4'], 'remaining_work': [], 'errors': []})},
        ]
        def respond(call, **kwargs):
            requests.append(json.loads(call.data))
            return io.BytesIO(json.dumps({'choices': [{'message': replies.pop(0)}]}).encode())
        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1', 'HARNESS_MODEL': 'fake'}, clear=True), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=respond), \
             patch('popot_agents.worker.http_harness.execute_tool', return_value='4') as execute, \
             redirect_stderr(io.StringIO()):
            run_http('Calculate and verify', {'tools': ['calculate'], 'max_tool_rounds': 1}, state=checkpoint)
            self.assertEqual(checkpoint['outcome']['status'], 'partial')
            restored = json.loads(json.dumps(checkpoint))
            answer = run_http('Continue verification', {'tools': ['calculate']}, state=restored)
        self.assertEqual(answer, 'Verified the earlier evidence: 4.')
        self.assertEqual(restored['outcome']['status'], 'completed')
        self.assertIn({'role': 'tool', 'tool_call_id': 'proof', 'content': '4'}, requests[-1]['messages'])
        self.assertNotIn('tool_choice', requests[-1])
        execute.assert_called_once()

    def test_every_role_gets_one_final_answer_after_its_tool_round_budget(self):
        for role in ({}, {'_ax_role': 'backend_engineer'}, {'_ax_role': 'qa_engineer'},
                     {'_ax_role': 'code_reviewer'}, {'name': 'project_operator'}):
            with self.subTest(role=role):
                requests = []
                def respond(call, **kwargs):
                    body = json.loads(call.data)
                    requests.append(body)
                    message = ({'content': 'Verified checks; remaining work is incomplete.'}
                               if body.get('tool_choice') == 'none' else
                               {'tool_calls': [tool_call(f'call-{len(requests)}', 'calculate',
                                                        {'expression': '1+1'})]})
                    return io.BytesIO(json.dumps({'choices': [{'message': message}]}).encode())
                with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1', 'HARNESS_MODEL': 'fake'}, clear=True), \
                     patch('popot_agents.worker.http_harness.request.urlopen', side_effect=respond), \
                     patch('popot_agents.worker.http_harness.execute_tool', return_value='2') as execute, \
                     redirect_stderr(io.StringIO()):
                    answer = run_http('task', {**role, 'tools': ['calculate'], 'max_tool_rounds': 2})
                self.assertIn('Partial result', answer)
                self.assertIn('Verified checks', answer)
                self.assertEqual(execute.call_count, 2)
                self.assertEqual(len(requests), 3)
                self.assertNotIn('tools', requests[-1])
                self.assertEqual(requests[-1]['tool_choice'], 'none')
                transcript = json.loads(requests[-1]['messages'][-1]['content'])
                self.assertEqual(transcript[-1]['content'], '2')

    def test_final_round_does_not_execute_requested_writes_or_capture_complete_memory(self):
        requests = []
        def respond(call, **kwargs):
            requests.append(json.loads(call.data))
            return io.BytesIO(json.dumps({'choices': [{'message': {'tool_calls': [
                tool_call(f'write-{len(requests)}', 'write_file', {'path': 'a.txt', 'content': 'fixture'})]
            }}]}).encode())
        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1', 'HARNESS_MODEL': 'fake'}, clear=True), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=respond), \
             patch('popot_agents.worker.http_harness.execute_tool', return_value='written') as execute, \
             patch('popot_agents.worker.http_harness.TurnMemory') as memory_class, \
             redirect_stderr(io.StringIO()):
            memory = memory_class.return_value
            memory.context.return_value = None
            memory.enabled = False
            memory.run_complete = True
            answer = run_http('task', {'tools': ['write_file'], 'max_tool_rounds': 1})
        execute.assert_called_once()
        self.assertIn('Partial result', answer)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[-1]['tool_choice'], 'none')
        self.assertNotIn('tools', requests[-1])
        memory.finish_session.assert_called_once_with(False)
        memory.save.assert_not_called()

    def test_reviewer_returns_partial_status_after_its_tool_rounds_without_more_tools(self):
        requests = []
        def respond(call, **kwargs):
            body = json.loads(call.data)
            requests.append(body)
            message = ({'content': 'Review remains pending; nothing was published.'}
                       if body.get('tool_choice') == 'none' else
                       {'tool_calls': [tool_call('calc', 'calculate', {'expression': '1+1'})]})
            return io.BytesIO(json.dumps({'choices': [{'message': message}]}).encode())
        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1', 'HARNESS_MODEL': 'fake'}, clear=True), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=respond), \
             redirect_stderr(io.StringIO()):
            answer = run_http('Review PR', {'_ax_role': 'code_reviewer', 'tools': ['calculate'], 'max_tool_rounds': 2})
        self.assertIn('Partial result', answer)
        self.assertIn('Review remains pending', answer)
        self.assertEqual(len(requests), 3)
        self.assertEqual(requests[-1]['tool_choice'], 'none')
        self.assertNotIn('tools', requests[-1])

    def test_operator_gets_an_explicit_no_tools_answer_after_its_tool_rounds(self):
        requests=[]
        def respond(call,**kwargs):
            body=json.loads(call.data);requests.append(body)
            message=({'content':'Verified the current state; investigation is incomplete.'}
                     if body.get('tool_choice')=='none' else
                     {'tool_calls':[tool_call('calc','calculate',{'expression':'1+1'})]})
            return io.BytesIO(json.dumps({'choices':[{'message':message}]}).encode())
        with patch.dict(os.environ,{'HARNESS_BASE_URL':'http://fake/v1','HARNESS_MODEL':'fake'},clear=True), \
             patch('popot_agents.worker.http_harness.request.urlopen',side_effect=respond), \
             redirect_stderr(io.StringIO()):
            answer=run_http('Inspect project',{'name':'project_operator','tools':['calculate'],'max_tool_rounds':2})
        self.assertIn('Verified the current state',answer)
        self.assertEqual(len(requests),3)
        self.assertEqual(requests[-1]['tool_choice'],'none')
        self.assertNotIn('tools',requests[-1])

    def test_round_budget_uses_runtime_unless_role_overrides_it(self):
        reply = {'choices': [{'message': {'tool_calls': [
            tool_call('calc', 'calculate', {'expression': '1+1'})]}}]}
        for role, expected in (({}, 2), ({'max_tool_rounds': 3}, 3)):
            with self.subTest(role=role), \
                 patch.dict(RUNTIME['model'], {'default_max_tool_rounds': 2}), \
                 patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1', 'HARNESS_MODEL': 'fake'}, clear=True), \
                 patch('popot_agents.worker.http_harness.request.urlopen',
                       side_effect=lambda *a, **k: io.BytesIO(json.dumps(reply).encode())) as model, \
                 redirect_stderr(io.StringIO()):
                answer = run_http('task', {'tools': ['calculate'], **role})
                self.assertIn('Partial result', answer)
                self.assertEqual(model.call_count, expected + 1)

    def run_messages(self, messages, allowed):
        self.requests = []
        def reply(call, **kwargs):
            self.requests.append(json.loads(call.data))
            return io.BytesIO(json.dumps({'choices': [{'message': messages.pop(0)}]}).encode())
        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1', 'HARNESS_MODEL': 'fake'}, clear=True), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=reply), \
             redirect_stderr(io.StringIO()):
            return run_http('task', {'tools': allowed, 'max_tool_rounds': 4})

    def test_invalid_arguments_do_not_discard_other_tool_results(self):
        for arguments in ('{bad-json', '[]', {'expression': '1/0'}, {'expression': 5}):
            with self.subTest(arguments=arguments):
                answer = self.run_messages([
                    {'tool_calls': [tool_call('ok', 'calculate', {'expression': '2+1'}),
                                    tool_call('bad', 'calculate', arguments)]},
                    {'tool_calls': [tool_call('fixed', 'calculate', {'expression': '1+1'})]},
                    {'content': 'done'}], ['calculate'])
                self.assertEqual(answer, 'done')
                results = self.requests[1]['messages'][-2:]
                self.assertEqual(results[0]['content'], '3')
                self.assertEqual(results[1]['tool_call_id'], 'bad')
                self.assertTrue(json.loads(results[1]['content'])['isError'])
                self.assertEqual(self.requests[2]['messages'][-1]['content'], '2')

    def test_unknown_tool_is_recoverable_without_executing_it_or_losing_other_results(self):
        events = []
        with observe_progress(events.append):
            answer = self.run_messages([
                {'tool_calls': [tool_call('bad', 'mcp__github__pull_request_review_read', {}),
                                tool_call('ok', 'calculate', {'expression': '2+1'})]},
                {'content': 'Unavailable tool; valid result is 3.'}], ['calculate'])
        self.assertEqual(answer, 'Unavailable tool; valid result is 3.')
        results = self.requests[1]['messages'][-2:]
        self.assertEqual(results[0]['tool_call_id'], 'bad')
        error = json.loads(results[0]['content'])
        self.assertTrue(error['isError'])
        self.assertIn('calculate', error['error'])
        self.assertEqual(results[1]['content'], '3')
        event = next(json.loads(item) for item in events if json.loads(item)['event'] == 'tool_error')
        self.assertTrue(event['recoverable'])
        self.assertIn('not allowed', event['error'])

    def test_failed_file_operation_can_be_repaired_by_next_step(self):
        failure = subprocess.CompletedProcess([], 2, b'', b'file not found')
        success = subprocess.CompletedProcess([], 0, b'file contents', b'')
        with patch.object(tools, 'subprocess') as process:
            process.run.side_effect = [failure, success]
            self.assertEqual(self.run_messages([
                {'tool_calls': [tool_call('missing', 'read_file', {'path': 'missing.txt'})]},
                {'tool_calls': [tool_call('fixed', 'read_file', {'path': 'README.md'})]},
                {'content': 'done'}], ['read_file']), 'done')
        self.assertTrue(json.loads(self.requests[1]['messages'][-1]['content'])['isError'])

    def test_invalid_input_to_each_local_tool_is_recoverable_before_execution(self):
        cases = [('bash', {'command': 7}), ('read_file', {'path': '../escape'}),
                 ('write_file', {'path': 'x', 'content': 2}),
                 ('download_file', {'url': 'file:///etc/passwd', 'path': 'x'}),
                 ('utc_time', {'extra': True})]
        for name, arguments in cases:
            with self.subTest(name=name), patch.object(tools, '_run_command') as command, \
                 patch.object(tools, '_file_action') as file_action:
                self.assertEqual(self.run_messages([
                    {'tool_calls': [tool_call('bad', name, arguments)]},
                    {'content': 'Need a valid argument.'}], [name]), 'Need a valid argument.')
                self.assertTrue(json.loads(self.requests[1]['messages'][-1]['content'])['isError'])
                command.assert_not_called()
                file_action.assert_not_called()


class CliLoopBudgetTests(unittest.TestCase):
    def test_cli_uses_shorter_per_message_timeout(self):
        for requested, expected in ((3, 3), (900, 30)):
            with self.subTest(requested=requested), patch.dict(os.environ, {
                'HARNESS_COMMAND_JSON': '["example"]', 'HARNESS_TURN_TIMEOUT_SECONDS': '30',
            }, clear=True), patch('popot_agents.worker.agent_worker.subprocess.run') as run:
                run.return_value = subprocess.CompletedProcess([], 0, 'done', '')
                self.assertEqual(run_harness('task', timeout_seconds=requested), 'done')
                self.assertEqual(run.call_args.kwargs['timeout'], expected)

    def test_cli_inherits_runtime_rounds_when_role_omits_override(self):
        with patch.dict(RUNTIME['model'], {'default_max_tool_rounds': 7}), \
             patch.dict(os.environ, {
                 'HARNESS_COMMAND_JSON': '["claude", "--print", "--max-turns", "{max_turns}"]',
             }, clear=True), patch('popot_agents.worker.agent_worker.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, 'done', '')
            self.assertEqual(run_harness('task'), 'done')
            self.assertEqual(run.call_args.args[0][-1], '7')

    def test_cli_gets_role_round_budget_and_turn_timeout(self):
        with patch.dict(os.environ, {
            'HARNESS_COMMAND_JSON': '["claude", "--print", "--max-turns", "{max_turns}", "{task}"]',
            'HARNESS_ROLE_JSON': '{"max_tool_rounds":12}', 'HARNESS_TURN_TIMEOUT_SECONDS': '17',
        }, clear=True), patch('popot_agents.worker.agent_worker.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, 'done', '')
            self.assertEqual(run_harness('task'), 'done')
            self.assertEqual(run.call_args.args[0], ['claude', '--print', '--max-turns', '12', 'task'])
            self.assertEqual(run.call_args.kwargs['timeout'], 17)

    def test_cli_timeout_is_reported_without_restarting_process(self):
        with patch.dict(os.environ, {'HARNESS_COMMAND_JSON': '["example"]'}, clear=True), \
             patch('popot_agents.worker.agent_worker.subprocess.run',
                   side_effect=subprocess.TimeoutExpired('example', 60)) as run:
            with self.assertRaisesRegex(RuntimeError, 'CLI harness turn timed out'):
                run_harness('task')
            run.assert_called_once()

"""Regressions from a review published before its MCP response failed."""
import io
import json
import os
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch

from popot_agents.worker import mcp_client
from popot_agents.worker.http_harness import run_http
from popot_agents.worker.delegation_policy import delegation_instructions


class AgentReliabilityTests(unittest.TestCase):
    def test_review_routing_depends_on_available_role_and_current_owner(self):
        from ax_local.worker.delegation import make_tools
        for role, allowed, delegate_review in (
                ('backend_engineer', ['code_reviewer'], True),
                ('backend_engineer', ['qa_engineer'], False),
                ('code_reviewer', ['qa_engineer', 'analyst'], False)):
            with self.subTest(role=role, allowed=allowed):
                tools = make_tools({'AX_DELEGATION_URL': 'http://fake', 'AX_DELEGATION_TOKEN': 'test',
                                    'AX_DELEGATION_ROLES': json.dumps(allowed)})
                prompt = delegation_instructions({'name': role}, tools)
                self.assertEqual('delegate before performing your own full review' in prompt, delegate_review)
                if role == 'code_reviewer':
                    self.assertIn('You own the review', prompt)

    def run_sequence(self, replies, *, delegate=None, clock=None, timeout=900, role=None, state=None, env=None):
        from ax_local.worker.delegation import make_tools
        bodies = []
        tools = make_tools({'AX_DELEGATION_URL': 'http://fake', 'AX_DELEGATION_TOKEN': 'test',
                            'AX_DELEGATION_ROLES': '["qa_engineer"]'}) if delegate else {}
        if delegate:
            tools['delegate_task']['call'] = delegate
        state = {} if state is None else state
        def response(req, **kwargs):
            bodies.append(json.loads(req.data))
            message = replies.pop(0)
            if callable(message):
                message = message()
            return io.BytesIO(json.dumps({'choices': [{'message': message}]}).encode())
        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1', 'HARNESS_MODEL': 'fake',
                                    'HARNESS_TURN_TIMEOUT_SECONDS': str(timeout), **(env or {})}, clear=True), \
             patch('popot_agents.worker.http_harness.time.monotonic', side_effect=lambda: clock[0] if clock else 0), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=response), \
             redirect_stderr(io.StringIO()):
            answer = run_http('Verify the requested work', role or {'tools': ['calculate']},
                              extra_tools=tools, state=state)
        return answer, state, bodies

    @staticmethod
    def result(status='completed', answer='Result verified', remaining=None, errors=None):
        return {'content': json.dumps({'status': status, 'answer': answer,
            'evidence': ['verified tool evidence'], 'remaining_work': remaining or [], 'errors': errors or []})}

    @staticmethod
    def delegate_call():
        return {'tool_calls': [{'id': 'qa', 'function': {'name': 'delegate_task',
            'arguments': json.dumps({'role': 'qa_engineer', 'task': 'Run the required tests'})}}]}

    def test_partial_parent_result_finishes_without_forcing_another_child_run(self):
        from unittest.mock import Mock
        delegate = Mock(return_value=json.dumps({'sessionId': 'a'*16, 'role': 'qa_engineer',
            'state': 'TASK_STATE_COMPLETED', 'resumable': True,
            'answer': 'Old nested answer.\nIncomplete subtasks:\nPrevious child answer:\n' + 'noise '*1000,
            'outcome': {'status': 'partial', 'evidence': ['review-123 published'],
                        'remaining_work': ['run integration tests'], 'errors': []}}))
        answer, state, bodies = self.run_sequence([self.delegate_call(),
            self.result('partial', 'Published review-123; integration tests remain.', ['run integration tests'])],
            delegate=delegate)
        self.assertEqual(len(bodies), 2)
        delegate.assert_called_once()
        self.assertIn('Published review-123', answer)
        self.assertNotIn('Previous child answer', answer)
        self.assertLess(len(answer), 1800)
        self.assertEqual(state['outcome']['status'], 'partial')
        self.assertIn('review-123 published', state['outcome']['evidence'])
        self.assertEqual(state['outcome']['remaining_work'], ['run integration tests'])
        self.assertIn('noise', state['failed_delegations'][0][1]['answer'])

    def test_blank_result_items_are_not_unfinished_requirements(self):
        answer, state, _ = self.run_sequence([
            self.result(remaining=['', '  '], errors=['\n'])])
        self.assertEqual(state['outcome']['status'], 'completed')
        self.assertEqual(state['outcome']['remaining_work'], [])
        self.assertEqual(state['outcome']['errors'], [])

    def test_explicit_partial_is_never_promoted_by_cleaning_empty_items(self):
        _, state, _ = self.run_sequence([self.result('partial', remaining=[''], errors=[' '])])
        self.assertEqual(state['outcome']['status'], 'partial')
        self.assertEqual(state['outcome']['errors'], [])
        self.assertTrue(all(item.strip() for item in state['outcome']['remaining_work']))
        self.assertTrue(state['outcome']['remaining_work'])

    def test_short_remaining_budget_uses_final_response_in_long_and_resumed_turns(self):
        for timeout, elapsed in ((900, 774), (35, 20)):
            with self.subTest(timeout=timeout):
                clock = [0]
                def first():
                    clock[0] = elapsed
                    return {'tool_calls': [{'id': 'calc', 'function': {'name': 'calculate',
                            'arguments': '{"expression":"2+2"}'}}]}
                _, state, bodies = self.run_sequence([first, self.result(answer='4')],
                                                     clock=clock, timeout=timeout)
                self.assertEqual(bodies[-1].get('tool_choice'), 'none')
                self.assertEqual(state['outcome']['status'], 'completed')

    def test_delegate_is_not_launched_with_less_than_a_minute_of_work_budget(self):
        from unittest.mock import Mock
        clock = [0]
        def first():
            clock[0] = 740  # 40s work remain, plus the parent's 120s final reserve.
            return self.delegate_call()
        delegate = Mock(return_value=json.dumps({'sessionId': 'a'*16, 'role': 'qa_engineer',
                         'state': 'TASK_STATE_COMPLETED', 'answer': 'done'}))
        _, state, bodies = self.run_sequence([first, self.result('partial', remaining=['run tests'])],
                                             delegate=delegate, clock=clock)
        delegate.assert_not_called()
        error = json.loads(bodies[-1]['messages'][-1]['content'])
        self.assertIn('60 seconds', error['error'])
        self.assertEqual(len(state['failed_delegations']), 1)
        self.assertIsNone(state['failed_delegations'][0][1]['session_id'])

    def test_refused_child_cannot_be_reported_as_completed(self):
        from unittest.mock import Mock
        clock = [0]
        def first():
            clock[0] = 740
            return self.delegate_call()
        delegate = Mock(return_value='{}')
        answer, state, _ = self.run_sequence([first, self.result(answer='All tests passed')],
                                             delegate=delegate, clock=clock)
        delegate.assert_not_called()
        self.assertEqual(state['outcome']['status'], 'partial')
        self.assertNotIn('All tests passed', answer)

    def test_repeated_invalid_final_output_stops_after_one_correction(self):
        answer, state, bodies = self.run_sequence([
            {'content': 'Published review-123, but this is not the required JSON.'},
            {'content': 'Still not JSON.'}])
        self.assertEqual(len(bodies), 2)
        self.assertEqual(state['outcome']['status'], 'partial')
        self.assertIn('invalid final result', state['outcome']['errors'][0])

    def test_repeated_premature_completion_does_not_loop_until_budget_exhaustion(self):
        from unittest.mock import Mock
        delegate = Mock(return_value=json.dumps({'sessionId': 'a'*16, 'role': 'qa_engineer',
            'state': 'TASK_STATE_COMPLETED', 'resumable': True, 'answer': 'Tests still pending',
            'outcome': {'status': 'partial', 'evidence': [], 'remaining_work': ['run tests'], 'errors': []}}))
        answer, state, bodies = self.run_sequence([
            self.delegate_call(), self.result(answer='All done'), self.result(answer='All done')],
            delegate=delegate)
        self.assertEqual(len(bodies), 3)
        delegate.assert_called_once()
        self.assertNotIn('All done', answer)
        self.assertEqual(state['outcome']['status'], 'partial')

    def test_refused_continuation_preserves_resumable_child_and_original_evidence(self):
        from unittest.mock import Mock
        clock = [0]
        delegate = Mock(return_value=json.dumps({'sessionId': 'a'*16, 'role': 'qa_engineer',
            'state': 'TASK_STATE_COMPLETED', 'resumable': True, 'answer': 'Read-back remains',
            'outcome': {'status': 'partial', 'evidence': ['review-123 may be published'],
                        'remaining_work': ['verify review-123'], 'errors': []}}))
        def resume():
            clock[0] = 740
            call = self.delegate_call()
            call['tool_calls'][0]['function']['arguments'] = json.dumps({
                'role': 'qa_engineer', 'task': 'Verify existing review-123', 'session_id': 'a'*16})
            return call
        _, state, _ = self.run_sequence([self.delegate_call(), resume,
            self.result('partial', remaining=['verify review-123'])], delegate=delegate, clock=clock)
        delegate.assert_called_once()
        failure = state['failed_delegations'][0][1]
        self.assertTrue(failure['resumable'])
        self.assertEqual(failure['session_id'], 'a'*16)
        self.assertIn('review-123 may be published', state['outcome']['evidence'])

    def test_completed_child_is_not_resumed_to_request_another_summary(self):
        from unittest.mock import Mock
        done = {'sessionId': 'a'*16, 'role': 'qa_engineer', 'state': 'TASK_STATE_COMPLETED',
                'answer': 'Tests verified, report: artifact-123', 'outcome': {'status': 'completed',
                'evidence': ['artifact-123'], 'remaining_work': [], 'errors': []}}
        delegate = Mock(return_value=json.dumps(done))
        resume = self.delegate_call()
        resume['tool_calls'][0]['function']['arguments'] = json.dumps({
            'role': 'qa_engineer', 'task': 'Confirm your final summary', 'session_id': 'a'*16})
        _, state, bodies = self.run_sequence([self.delegate_call(), resume, self.result()], delegate=delegate)
        delegate.assert_called_once()
        reused = json.loads(bodies[-1]['messages'][-1]['content'])
        self.assertTrue(reused['continuation_skipped'])
        self.assertEqual(reused['answer'], done['answer'])
        self.assertIn('no new work', reused['notice'])
        self.assertEqual(state['outcome']['status'], 'completed')

    def test_completed_child_result_survives_parent_checkpoint(self):
        from unittest.mock import Mock
        done = {'sessionId': 'a'*16, 'role': 'qa_engineer', 'state': 'TASK_STATE_COMPLETED',
                'answer': 'Tests verified', 'outcome': {'status': 'completed',
                'evidence': ['test report'], 'remaining_work': [], 'errors': []}}
        delegate = Mock(return_value=json.dumps(done))
        _, state, _ = self.run_sequence([self.delegate_call(),
            self.result('partial', remaining=['verify another requirement'])], delegate=delegate)
        checkpoint = json.loads(json.dumps(state))
        resume = self.delegate_call()
        resume['tool_calls'][0]['function']['arguments'] = json.dumps({
            'role': 'qa_engineer', 'task': 'Return your previous result', 'session_id': 'a'*16})
        _, state, _ = self.run_sequence([resume, self.result('partial', remaining=['verify another requirement'])],
                                       delegate=delegate, state=checkpoint)
        delegate.assert_called_once()
        self.assertEqual(state['completed_delegations']['a'*16], done)
        self.assertEqual(state['outcome']['status'], 'partial')

    def test_mcp_cleanup_failure_preserves_the_received_result_without_repeating_write(self):
        calls = []
        class Client:
            def __init__(self, *args): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): raise RuntimeError('connection closed during cleanup')
            async def call_tool(self, name, arguments):
                calls.append(name)
                return SimpleNamespace(is_error=False, content=[SimpleNamespace(
                    type='text', text='Review submitted: review-123')])
        with patch('mcp.Client', Client), redirect_stderr(io.StringIO()):
            output = mcp_client.call_tool({}, 'submit_review', {})
        self.assertEqual(output, 'Review submitted: review-123')
        self.assertEqual(calls, ['submit_review'])

    def test_transport_failure_returns_to_the_same_agent_for_readback(self):
        body_log, operations, state = [], [], {}
        names = ('publish', 'read_back')
        discovered = {'mcp__remote__'+name: {'server': 'remote', 'native_name': name,
            'schema': {'type': 'function', 'function': {'name': 'mcp__remote__'+name,
                       'parameters': {'type': 'object'}}}} for name in names}
        report = {'status': 'completed', 'answer': 'Publication verified: review-123',
                  'evidence': ['read_back: review-123 published'], 'remaining_work': [], 'errors': []}
        def response(req, **kwargs):
            body_log.append(json.loads(req.data))
            index = len(body_log)-1
            message = ({'tool_calls': [{'id': str(index), 'type': 'function', 'function': {
                'name': 'mcp__remote__'+names[index], 'arguments': '{}'}}]} if index < 2
                else {'content': json.dumps(report)})
            return io.BytesIO(json.dumps({'choices': [{'message': message}]}).encode())
        async def remote(config, name, arguments):
            operations.append(name)
            if name == 'publish': raise OSError('response disconnected after write')
            return 'review-123 published'
        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1', 'HARNESS_MODEL': 'fake'}, clear=True), \
             patch.object(mcp_client, 'discover_tools', return_value=discovered), \
             patch.object(mcp_client, '_call_one', side_effect=remote), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=response), \
             redirect_stderr(io.StringIO()):
            answer = run_http('Publish and verify', {'mcpServers': {'remote': {}}}, state=state)
        self.assertEqual(answer, report['answer'])
        self.assertEqual(operations, ['publish', 'read_back'])
        error = json.loads(body_log[1]['messages'][-1]['content'])
        self.assertTrue(error['isError'])
        self.assertTrue(error['outcome_unknown'])
        self.assertEqual(state['outcome']['status'], 'completed')

    def test_last_round_can_report_verified_completion_but_not_unverified_completion(self):
        for evidence, expected in ((['result=4'], 'completed'), ([], 'partial')):
            with self.subTest(evidence=evidence):
                bodies, state = [], {}
                def response(req, **kwargs):
                    bodies.append(json.loads(req.data))
                    msg = ({'tool_calls': [{'id': 'c1', 'type': 'function', 'function': {
                        'name': 'calculate', 'arguments': '{"expression":"2+2"}'}}],
                        'reasoning_content': 'Long provider reasoning, not final evidence'} if len(bodies) == 1
                        else {'content': json.dumps({'status': 'completed', 'answer': '4',
                            'evidence': evidence, 'remaining_work': [], 'errors': []})})
                    return io.BytesIO(json.dumps({'choices': [{'message': msg}]}).encode())
                with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1', 'HARNESS_MODEL': 'fake'}, clear=True), \
                     patch('popot_agents.worker.http_harness.request.urlopen', side_effect=response), \
                     redirect_stderr(io.StringIO()):
                    run_http('Calculate 2+2', {'tools': ['calculate'], 'max_tool_rounds': 1}, state=state)
                self.assertEqual(state['outcome']['status'], expected)
                self.assertNotIn('Long provider reasoning', bodies[-1]['messages'][-1]['content'])
                self.assertEqual(bodies[-1]['tool_choice'], 'none')

    def test_short_work_budget_goes_straight_to_final_instead_of_subsecond_request(self):
        clock, bodies, state = [0], [], {}
        def response(req, **kwargs):
            bodies.append(json.loads(req.data))
            clock[0] = 38.9  # 60s turn, 20s reserve; 1.1s of work would remain.
            message = ({'tool_calls': [{'id': 'c', 'function': {
                'name': 'calculate', 'arguments': '{"expression":"2+2"}'}}]} if len(bodies) == 1
                else {'content': json.dumps({'status': 'completed', 'answer': '4',
                    'evidence': ['4'], 'remaining_work': [], 'errors': []})})
            return io.BytesIO(json.dumps({'choices': [{'message': message}]}).encode())
        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1', 'HARNESS_MODEL': 'fake',
                                    'HARNESS_TURN_TIMEOUT_SECONDS': '60'}, clear=True), \
             patch('popot_agents.worker.http_harness.time.monotonic', side_effect=lambda: clock[0]), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=response), \
             redirect_stderr(io.StringIO()):
            run_http('Calculate', {'tools': ['calculate']}, state=state)
        self.assertEqual(bodies[1].get('tool_choice'), 'none')
        self.assertEqual(len(bodies), 2)

    def test_explicit_replacement_carries_prior_decisions_and_resolves_only_its_failure(self):
        from unittest.mock import Mock
        from ax_local.worker.delegation import make_tools
        old, new = 'a'*16, 'b'*16
        for resumable in (False, True):
            with self.subTest(resumable=resumable):
                state, bodies = {}, []
                tools = make_tools({'AX_DELEGATION_URL': 'http://fake', 'AX_DELEGATION_TOKEN': 'test',
                                    'AX_DELEGATION_ROLES': '["code_reviewer"]'})
                failed = {'sessionId': old, 'role': 'code_reviewer', 'state': 'TASK_STATE_FAILED',
                          'isError': True, 'error': 'response lost', 'resumable': resumable,
                          'answer': 'Rejected G: hypothetical; review 123 may be published.',
                          'outcome': {'status': 'partial', 'evidence': ['G: hypothetical'],
                                      'remaining_work': ['verify review 123'], 'errors': []}}
                done = {'sessionId': new, 'role': 'code_reviewer', 'state': 'TASK_STATE_COMPLETED',
                        'answer': 'Review 123 verified. G remains rejected.', 'outcome': {
                        'status': 'completed', 'evidence': ['review 123 verified'], 'remaining_work': [], 'errors': []}}
                delegate = Mock(side_effect=[json.dumps(failed), json.dumps(done)])
                tools['delegate_task']['call'] = delegate
                arguments = [{'role': 'code_reviewer', 'task': 'Review PR'},
                             {'role': 'code_reviewer', 'task': 'Verify existing review', 'replaces_session_id': old}]
                report = {'status': 'partial' if resumable else 'completed', 'answer': 'Review verified',
                          'evidence': ['review 123 verified'], 'remaining_work': [], 'errors': []}
                def response(req, **kwargs):
                    bodies.append(json.loads(req.data))
                    index = len(bodies)-1
                    msg = ({'tool_calls': [{'id': str(index), 'function': {'name': 'delegate_task',
                           'arguments': json.dumps(arguments[index])}}]} if index < 2
                           else {'content': json.dumps(report)})
                    return io.BytesIO(json.dumps({'choices': [{'message': msg}]}).encode())
                with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1', 'HARNESS_MODEL': 'fake'}, clear=True), \
                     patch('popot_agents.worker.http_harness.request.urlopen', side_effect=response), \
                     redirect_stderr(io.StringIO()):
                    run_http('Review PR', {'name': 'backend_engineer', 'max_tool_rounds': 2},
                             extra_tools=tools, state=state)
                self.assertEqual(delegate.call_count, 1 if resumable else 2)
                if not resumable:
                    sent = delegate.call_args.args[0]
                    self.assertNotIn('replaces_session_id', sent)
                    self.assertIn('Rejected G: hypothetical', sent['task'])
                    self.assertEqual(state['failed_delegations'], [])
                    self.assertEqual(state['outcome']['status'], 'completed')
                else:
                    self.assertEqual(state['outcome']['status'], 'partial')

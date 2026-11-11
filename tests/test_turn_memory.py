import io
import json
import os
import unittest
from unittest.mock import Mock, patch

from popot_agents.worker.turn_memory import TurnMemory, project_dataset, scrub, parse_summary
from popot_agents.worker.http_harness import run_http
from popot_agents.runtime_config import RUNTIME

ROLE = {'mcpServers': {'cognee': {'command': ['python', 'memory.py'],
                                 'tools': ['remember', 'recall', 'memory_status']}}}
ENV = {'HARNESS_BASE_URL': 'http://model/v1', 'HARNESS_MODEL': 'example',
       'HARNESS_TURN_TIMEOUT_SECONDS': '60', 'COGNEE_PASSWORD': 'private-password',
       'HARNESS_TOOL_ENV_NAMES_JSON': '["COGNEE_PASSWORD"]'}
TASK = 'Review https://github.com/Owner/Repo/pull/68'


class TurnMemoryTests(unittest.TestCase):
    def test_observed_tool_results_keep_trace_status_and_only_eligible_evidence(self):
        outputs = [('plain output', False), ('{invalid', False), ('[]', False),
                   ('null', False), ('false', False), ('1', False), ('"text"', False),
                   ('{}', False), ('{"isError": false}', False), ('{"isError": 0}', False),
                   ('{"isError": null}', False), ('{"isError": []}', False),
                   ('{"isError": true}', True), ('{"isError": "failed"}', True),
                   ('{"isError": 1}', True), ('{"isError": ["failed"]}', True)]
        for name in ('bash', 'mcp__cognee__recall'):
            for output, is_error in outputs:
                with self.subTest(name=name, output=output), patch.dict(os.environ, ENV, clear=True):
                    memory = TurnMemory(ROLE, [{'role': 'user', 'content': TASK}], client=Mock())
                    memory.observe('call', name, {}, output)
                    self.assertEqual(memory.tools, [{'name': name,
                        'status': 'error' if is_error else 'success', 'output_chars': len(output)}])
                    expected = {'user': TASK}
                    if name == 'bash' and not is_error:
                        expected['call'] = output
                    self.assertEqual(memory.evidence, expected)

    def test_observed_evidence_stays_bounded_after_the_tool_trace_is_full(self):
        with patch.dict(os.environ, ENV, clear=True), \
             patch.dict(RUNTIME['memory'], {'trace_tool_limit': 1, 'tool_evidence_count': 2}):
            memory = TurnMemory(ROLE, [{'role': 'user', 'content': TASK}], client=Mock())
            for call_id in ('first', 'second', 'third'):
                memory.observe(call_id, 'bash', {}, call_id + ' private-password')
            memory.observe('error', 'bash', {}, '{"isError": true}')
            self.assertEqual(memory.evidence, {'user': TASK,
                'second': 'second [REDACTED]', 'third': 'third [REDACTED]'})
            self.assertEqual(memory.tools, [{'name': 'bash', 'status': 'success',
                                            'output_chars': len('first private-password')}])

    def test_automatic_continuation_keeps_authorized_memory_project(self):
        from popot_agents.orchestrator.workflow import WorkflowRun
        workflow = WorkflowRun(RUNTIME['workflow'], TASK)
        workflow.begin()
        workflow.record({'answer': 'continue', 'outcome': {
            'status': 'partial', 'reason': 'agent_reported',
            'evidence': ['Dependency: https://github.com/Vendor/Library/pull/9'],
            'remaining_work': ['Finish the review'], 'errors': []}}, {'messages': []})
        messages = [{'role': 'user', 'content': TASK},
                    {'role': 'assistant', 'content': 'Dependency identified'},
                    {'role': 'user', 'content': workflow.continuation(compact=True)}]
        client = Mock()
        client.recall.return_value = []
        replies = ['done', json.dumps({'memories': [{
            'kind': 'constraint', 'text': 'Review the requested PR',
            'source': 'user', 'quote': 'Review'}]})]
        with patch.dict(os.environ, ENV, clear=True), \
             patch('popot_agents.worker.turn_memory.CogneeMemory', return_value=client), \
             patch('popot_agents.worker.http_harness.mcp_client.discover_tools', return_value={}), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=lambda *a, **kw:
                 io.BytesIO(json.dumps({'choices': [{'message': {'content': replies.pop(0)}}]}).encode())):
            self.assertEqual(run_http(messages, ROLE, run_context={
                'authorization': workflow.meta['authorization'],
                'assignments': ['Check https://github.com/Vendor/Library/pull/9 for the cause']}), 'done')
        dataset = project_dataset([{'role': 'user', 'content': TASK}])
        self.assertEqual(client.recall.call_args.args[1], dataset)
        self.assertEqual(client.remember.call_args.args[1], dataset)
        self.assertEqual(json.loads(client.remember.call_args.args[0])['references'],
                         ['https://github.com/Owner/Repo/pull/68'])

    def test_authorized_followups_and_ambiguity_control_continuation_memory(self):
        followup = 'Review https://github.com/Else/Other/pull/8'
        for authorization, expected in (
                ([TASK, 'Run checks too'], project_dataset([{'role': 'user', 'content': TASK}])),
                ([TASK, followup], project_dataset([{'role': 'user', 'content': followup}])),
                ([TASK + ' and ' + followup], None)):
            with self.subTest(authorization=authorization):
                client = Mock()
                client.recall.return_value = []
                replies = ['done', '{"memories": []}']
                with patch.dict(os.environ, ENV, clear=True), \
                     patch('popot_agents.worker.turn_memory.CogneeMemory', return_value=client) as constructor, \
                     patch('popot_agents.worker.http_harness.mcp_client.discover_tools', return_value={}), \
                     patch('popot_agents.worker.http_harness.request.urlopen', side_effect=lambda *a, **kw:
                         io.BytesIO(json.dumps({'choices': [{'message': {'content': replies.pop(0)}}]}).encode())):
                    self.assertEqual(run_http('Continue; evidence https://github.com/Vendor/Library/pull/9',
                        ROLE, run_context={'authorization': authorization}), 'done')
                if expected is None:
                    constructor.assert_not_called()
                else:
                    self.assertEqual(client.recall.call_args.args[1], expected)

    def test_await_tasks_checkpoint_closes_memory_turn_without_error(self):
        from tests.test_agent_reliability import AgentReliabilityTests
        from tests.test_async_delegation import call, pending
        helper = AgentReliabilityTests()
        client = Mock()
        client.recall.return_value = []
        with patch('popot_agents.worker.turn_memory.CogneeMemory', return_value=client), \
             patch('popot_agents.worker.http_harness.mcp_client.discover_tools', return_value={}):
            _, state, _ = helper.run_sequence([helper.delegate_call(),
                call('await_tasks', {'session_ids': ['a' * 16]}, 'join')],
                delegate=Mock(return_value=pending()), env={**ENV, 'HARNESS_TURN_TIMEOUT_SECONDS': '900'},
                role={**ROLE, 'tools': ['calculate'], 'memory_project': 'popot-diagnostics'})
        event = client.session_event.call_args.args[0]
        self.assertEqual(state['outcome']['reason'], 'child_pending')
        self.assertEqual(event['status'], 'completed')
        self.assertEqual(event['reason'], 'child_pending')
        self.assertEqual(event['errors'], [])
        client.remember.assert_not_called()

    def test_checkpoint_is_not_failure_and_outcome_has_no_raw_context(self):
        for reason in ('child_pending', 'work_budget_exhausted'):
            client = Mock()
            with self.subTest(reason=reason), patch.dict(os.environ, ENV, clear=True):
                memory = TurnMemory(ROLE, [{'role': 'user', 'content': TASK}], client=client)
                memory.start_session()
                memory.outcome = {'status': 'partial', 'reason': reason, 'errors': [],
                                  'evidence': ['private output'], 'remaining_work': ['private task']}
                memory.finish_session(False)
            event = client.session_event.call_args.args[0]
            self.assertEqual(event['status'], 'completed')
            self.assertEqual(event['outcome'], 'partial')
            self.assertEqual(event['reason'], reason)
            self.assertNotIn('private output', json.dumps(event))
            self.assertNotIn('private task', json.dumps(event))

    def test_partial_model_failure_remains_failed_with_redacted_reason(self):
        client = Mock()
        with patch.dict(os.environ, ENV, clear=True):
            memory = TurnMemory(ROLE, [{'role': 'user', 'content': TASK}], client=client)
            memory.start_session()
            memory.outcome = {'status': 'partial', 'reason': 'work_budget_exhausted',
                              'errors': ['model timeout private-password']}
            memory.finish_session(False)
        event = client.session_event.call_args.args[0]
        self.assertEqual(event['status'], 'failed')
        self.assertEqual(event['outcome'], 'partial')
        self.assertIn('model timeout', event['errors'][0])
        self.assertNotIn('private-password', json.dumps(event))

    def test_returned_partial_is_forwarded_to_memory(self):
        client = Mock()
        client.recall.return_value = []
        report = {'status': 'partial', 'answer': 'checkpoint', 'evidence': [],
                  'errors': [], 'remaining_work': ['continue checks']}
        with patch.dict(os.environ, ENV, clear=True), \
             patch('popot_agents.worker.turn_memory.CogneeMemory', return_value=client), \
             patch('popot_agents.worker.http_harness.mcp_client.discover_tools', return_value={}), \
             patch('popot_agents.worker.http_harness.request.urlopen', return_value=io.BytesIO(
                 json.dumps({'choices': [{'message': {'content': json.dumps(report)}}]}).encode())):
            self.assertEqual(run_http(TASK, ROLE, state={}), 'checkpoint')
        event = client.session_event.call_args.args[0]
        self.assertEqual(event['status'], 'completed')
        self.assertEqual(event['outcome'], 'partial')

    def test_memory_outage_warns_and_main_model_still_completes(self):
        client = Mock()
        client.session_event.side_effect = OSError('private-password')
        state = {}
        content = json.dumps({'status':'completed', 'answer':'Main work verified',
            'evidence':['shell result'], 'errors':[], 'remaining_work':[]})
        with patch.dict(os.environ, ENV, clear=True), \
             patch('popot_agents.worker.turn_memory.CogneeMemory', return_value=client), \
             patch('popot_agents.worker.http_harness.mcp_client.discover_tools', return_value={}), \
             patch('popot_agents.worker.http_harness.request.urlopen', return_value=io.BytesIO(
                 json.dumps({'choices':[{'message':{'content':content}}]}).encode())):
            self.assertEqual(run_http(TASK, ROLE, state=state), 'Main work verified')
        self.assertEqual(state['outcome']['status'], 'completed')
        self.assertTrue(state['outcome']['warnings'])
        self.assertEqual(state['outcome']['errors'], [])
        self.assertNotIn('private-password', str(state))
        client.recall.assert_not_called()

    def test_gateway_capture_lowers_effort_and_preserves_room_for_thinking(self):
        client = Mock()
        client.recall.return_value = []
        bodies = []

        def respond(call, **kwargs):
            bodies.append(json.loads(call.data))
            content = 'done' if len(bodies) == 1 else '{"memories":[]}'
            return io.BytesIO(json.dumps({'choices': [{'finish_reason': 'stop',
                'message': {'content': content}}]}).encode())

        with patch.dict(os.environ, {**ENV, 'HARNESS_UPSTREAM_HOST': 'omniroute',
             'HARNESS_MODEL_PARAMETERS_JSON': json.dumps(
                 {'reasoning_effort': 'max', 'max_tokens': 65536})}, clear=True), \
             patch('popot_agents.worker.turn_memory.CogneeMemory', return_value=client), \
             patch('popot_agents.worker.http_harness.mcp_client.discover_tools', return_value={}), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=respond):
            self.assertEqual(run_http(TASK, ROLE), 'done')
        self.assertEqual(bodies[0]['reasoning_effort'], 'max')
        self.assertEqual(bodies[0]['max_tokens'], 65536)
        self.assertEqual(bodies[1]['reasoning_effort'], 'low')
        self.assertEqual(bodies[1]['max_tokens'], 65536)
        self.assertNotIn('response_format', bodies[1])

    def test_automatic_recall_passes_its_full_budget_to_the_memory_client(self):
        with patch.dict(os.environ, ENV, clear=True):
            client = Mock()
            client.recall.return_value = []
            memory = TurnMemory(ROLE, [{'role': 'user', 'content': TASK}], client=client)
            memory.context(20)
        self.assertEqual(client.timeout, 20)
        self.assertEqual(client.recall.call_args.kwargs['timeout_seconds'], 20)

    def test_capture_keeps_model_parameters_and_waits_within_the_turn_deadline(self):
        parameters = {'reasoning': {'enabled': False}, 'chat_template_kwargs': {'enable_thinking': False},
                      'max_tokens': 8192}
        for turn_seconds, expected_timeout in ((600, 300), (180, 175), (60, 55)):
            with self.subTest(turn_seconds=turn_seconds):
                client = Mock()
                client.recall.return_value = []
                bodies, timeouts = [], []
                def respond(call, *, timeout):
                    bodies.append(json.loads(call.data))
                    timeouts.append(timeout)
                    if len(bodies) == 1:
                        content = 'Review remains incomplete'
                    else:
                        if timeout < 40:
                            raise TimeoutError('provider needs 40 seconds')
                        content = json.dumps({'memories': [
                            {'kind': 'constraint', 'text': 'Review requested', 'source': 'user', 'quote': 'Review'}]})
                    return io.BytesIO(json.dumps({'choices': [{'message': {'content': content}}]}).encode())
                with patch.dict(RUNTIME['timeouts'], {'memory_summary_seconds': 300}), \
                     patch.dict(os.environ, {**ENV, 'HARNESS_TURN_TIMEOUT_SECONDS': str(turn_seconds),
                     'HARNESS_MODEL_PARAMETERS_JSON': json.dumps(parameters)}, clear=True), \
                     patch('popot_agents.worker.turn_memory.CogneeMemory', return_value=client), \
                     patch('popot_agents.worker.http_harness.mcp_client.discover_tools', return_value={}), \
                     patch('popot_agents.worker.http_harness.time.monotonic', return_value=0), \
                     patch('popot_agents.worker.http_harness.request.urlopen', side_effect=respond):
                    self.assertEqual(run_http(TASK, ROLE), 'Review remains incomplete')
                self.assertEqual(bodies[1].get('reasoning'), parameters['reasoning'])
                self.assertEqual(bodies[1].get('chat_template_kwargs'), parameters['chat_template_kwargs'])
                self.assertEqual(bodies[1].get('tool_choice'), 'none')
                self.assertEqual(bodies[1].get('response_format'), {'type': 'json_object'})
                self.assertEqual(bodies[1]['max_tokens'], 2000)
                self.assertEqual(timeouts[1], expected_timeout)
                client.remember.assert_called_once()

    def test_ollama_cloud_capture_disables_thinking_only_for_summary(self):
        client = Mock()
        client.recall.return_value = []
        bodies = []
        def respond(call, **kwargs):
            body = json.loads(call.data)
            bodies.append(body)
            content = 'done' if len(bodies) == 1 else '{"memories":[]}'
            return io.BytesIO(json.dumps({'choices': [{'finish_reason': 'stop',
                'message': {'content': content}}]}).encode())
        parameters = {'reasoning': {'effort': 'high'}, 'temperature': 0.7}
        with patch.dict(os.environ, {**ENV, 'HARNESS_BASE_URL': 'http://proxy.test/v1',
             'HARNESS_UPSTREAM_HOST': 'ollama.com',
             'HARNESS_MODEL_PARAMETERS_JSON': json.dumps(parameters)}, clear=True), \
             patch('popot_agents.worker.turn_memory.CogneeMemory', return_value=client), \
             patch('popot_agents.worker.http_harness.mcp_client.discover_tools', return_value={}), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=respond):
            self.assertEqual(run_http(TASK, ROLE), 'done')
        self.assertEqual(bodies[0]['reasoning'], {'effort': 'high'})
        self.assertEqual(bodies[0]['temperature'], 0.7)
        self.assertEqual(bodies[1]['reasoning_effort'], 'none')
        self.assertNotIn('reasoning', bodies[1])
        self.assertNotIn('response_format', bodies[1])
        self.assertEqual(bodies[1]['temperature'], 0)
        client.remember.assert_not_called()

    def test_truncated_summary_is_retried_even_if_its_json_parses(self):
        for recovered in (True, False):
            with self.subTest(recovered=recovered):
                client = Mock()
                client.recall.return_value = []
                note = json.dumps({'memories': [{'kind': 'constraint', 'text': 'Review requested',
                    'source': 'user', 'quote': 'Review'}]})
                replies = [{'choices': [{'message': {'content': 'done'}}]}] + [
                    {'choices': [{'finish_reason': reason, 'message': {
                        'content': note, 'reasoning': 'private reasoning must not be logged'}}]}
                    for reason in ('length', 'stop' if recovered else 'length')]
                with patch.dict(os.environ, ENV, clear=True), \
                     patch('popot_agents.worker.turn_memory.CogneeMemory', return_value=client), \
                     patch('popot_agents.worker.http_harness.mcp_client.discover_tools', return_value={}), \
                     patch('popot_agents.worker.http_harness._trace') as trace, \
                     patch('popot_agents.worker.http_harness.request.urlopen', side_effect=lambda *a, **kw:
                           io.BytesIO(json.dumps(replies.pop(0)).encode())) as model:
                    self.assertEqual(run_http(TASK, ROLE), 'done')
                self.assertEqual(model.call_count, 3)
                self.assertEqual(client.remember.call_count, int(recovered))
                response_events = [c.kwargs for c in trace.call_args_list
                                   if c.args[0] == 'memory_summary_response']
                self.assertEqual(response_events[0]['finish_reason'], 'length')
                self.assertEqual(response_events[0]['content_chars'], len(note))
                self.assertNotIn('private reasoning must not be logged', str(trace.call_args_list))
                self.assertIn('truncated_summary', str(trace.call_args_list))

    def test_recall_distinguishes_useful_empty_and_warming_up(self):
        for result, outcome, count in [
                ([{'text': 'Verified fact'}], 'useful', 1),
                ([], 'empty', 0),
                ({'status': 'empty', 'memories': []}, 'empty', 0),
                ([{'source': 'system', 'status': 'memory_warming_up',
                   'text': 'Memory is still warming up'}], 'warming_up', 0)]:
            with self.subTest(outcome=outcome), patch.dict(os.environ, ENV, clear=True), \
                 patch('popot_agents.worker.turn_memory.trace') as trace:
                client = Mock()
                client.recall.return_value = result
                memory = TurnMemory(ROLE, [{'role': 'user', 'content': TASK}], client=client)
                context = memory.context(60)
                self.assertEqual(bool(context), bool(count))
                self.assertEqual(memory.tools[-1]['recall_outcome'], outcome)
                self.assertEqual(memory.tools[-1]['chunks'], count)
                trace.assert_called_with('memory_recalled', dataset=memory.dataset,
                                         chunks=count, outcome=outcome)

    def test_incomplete_results_save_only_evidence_backed_findings(self):
        for status, rounds in (('partial', 40), ('blocked', 40), ('partial', 1)):
            with self.subTest(status=status, rounds=rounds):
                client = Mock()
                client.recall.return_value = []
                state = {}
                replies = [
                    {'tool_calls': [{'id': 'proof', 'function': {
                        'name': 'calculate', 'arguments': '{"expression":"2+2"}'}}]},
                    {'content': json.dumps({'status': status, 'answer': 'Verification pending',
                        'evidence': ['4'], 'remaining_work': ['publish'], 'errors': []})},
                    {'content': json.dumps({'memories': [
                        {'kind': 'check', 'text': 'Calculation returned 4', 'source': 'proof', 'quote': '4'},
                        {'kind': 'fact', 'text': 'Invented', 'source': 'proof', 'quote': 'not observed'}]})}]
                with patch.dict(os.environ, ENV, clear=True), \
                     patch('popot_agents.worker.turn_memory.CogneeMemory', return_value=client), \
                     patch('popot_agents.worker.http_harness.mcp_client.discover_tools', return_value={}), \
                     patch('popot_agents.worker.http_harness.request.urlopen', side_effect=lambda *a, **kw:
                           io.BytesIO(json.dumps({'choices': [{'message': replies.pop(0)}]}).encode())):
                    run_http(TASK, {**ROLE, 'tools': ['calculate'], 'max_tool_rounds': rounds}, state=state)
                client.remember.assert_called_once()
                record = json.loads(client.remember.call_args.args[0])
                self.assertEqual(record['task_status'], status)
                self.assertEqual(len(record['memories']), 1)
                self.assertEqual(state['outcome']['status'], status)
                self.assertEqual(client.session_event.call_args.args[0]['memories_saved'], 1)

    def test_blocked_result_survives_capture_timeout_or_exhausted_budget(self):
        for exhausted in (False, True):
            with self.subTest(exhausted=exhausted):
                client = Mock()
                client.recall.return_value = []
                state, clock = {}, [0]
                report = {'status': 'blocked', 'answer': 'Missing permission',
                          'evidence': [], 'remaining_work': ['publish'], 'errors': []}
                def respond(*args, **kwargs):
                    clock[0] = 55 if exhausted else 0
                    return io.BytesIO(json.dumps({'choices': [{'message': {
                        'content': json.dumps(report)}}]}).encode())
                with patch.dict(os.environ, ENV, clear=True), \
                     patch('popot_agents.worker.turn_memory.CogneeMemory', return_value=client), \
                     patch('popot_agents.worker.http_harness.mcp_client.discover_tools', return_value={}), \
                     patch('popot_agents.worker.http_harness.time.monotonic', side_effect=lambda: clock[0]), \
                     patch('popot_agents.worker.http_harness._trace') as trace, \
                     patch('popot_agents.worker.http_harness.request.urlopen', side_effect=respond) as model:
                    # The summary endpoint fails independently of the task response.
                    def dispatch(*args, **kwargs):
                        if model.call_count > 1:
                            raise TimeoutError()
                        return respond(*args, **kwargs)
                    model.side_effect = dispatch
                    self.assertEqual(run_http(TASK, ROLE, state=state), 'Missing permission')
                self.assertEqual(state['outcome']['status'], 'blocked')
                client.remember.assert_not_called()
                events = [call.args[0] for call in trace.call_args_list]
                self.assertIn('memory_skipped' if exhausted else 'memory_capture_failed', events)
                self.assertEqual(model.call_count, 1 if exhausted else 2)

    def test_summary_connection_reset_retries_once_without_repeating_memory_write(self):
        client = Mock()
        client.recall.return_value = []
        report = {'status': 'completed', 'answer': 'Review complete',
                  'evidence': ['checked'], 'remaining_work': [], 'errors': []}
        summary = {'memories': [{'kind': 'constraint', 'text': 'Review the PR',
                                 'source': 'user', 'quote': 'Review https://github.com/Owner/Repo/pull/68'}]}
        calls = []
        def respond(*args, **kwargs):
            calls.append(args[0])
            if len(calls) == 2:
                raise ConnectionResetError('model connection reset')
            content = report if len(calls) == 1 else summary
            return io.BytesIO(json.dumps({'choices': [{'message': {
                'content': json.dumps(content)}, 'finish_reason': 'stop'}]}).encode())
        with patch.dict(os.environ, ENV, clear=True), \
             patch('popot_agents.worker.turn_memory.CogneeMemory', return_value=client), \
             patch('popot_agents.worker.http_harness.mcp_client.discover_tools', return_value={}), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=respond), \
             patch('popot_agents.worker.http_harness._trace') as trace:
            self.assertEqual(run_http(TASK, ROLE, state={}), 'Review complete')
        self.assertEqual(len(calls), 3)
        client.remember.assert_called_once()
        self.assertEqual([call.args[0] for call in trace.call_args_list].count('memory_summary_retry'), 1)

    def test_summary_connection_reset_on_retry_is_raised_without_further_attempts(self):
        client = Mock()
        client.recall.return_value = []
        report = {'status': 'completed', 'answer': 'Review complete',
                  'evidence': ['checked'], 'remaining_work': [], 'errors': []}
        calls = []
        def respond(*args, **kwargs):
            calls.append(args[0])
            if len(calls) == 1:
                return io.BytesIO(json.dumps({'choices': [{'message': {
                    'content': json.dumps(report)}, 'finish_reason': 'stop'}]}).encode())
            raise ConnectionResetError('model connection reset')
        with patch.dict(os.environ, ENV, clear=True), \
             patch('popot_agents.worker.turn_memory.CogneeMemory', return_value=client), \
             patch('popot_agents.worker.http_harness.mcp_client.discover_tools', return_value={}), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=respond), \
             patch('popot_agents.worker.http_harness._trace') as trace:
            self.assertEqual(run_http(TASK, ROLE, state={}), 'Review complete')
        events = [call.args[0] for call in trace.call_args_list]
        self.assertEqual(len(calls), 3)
        client.remember.assert_not_called()
        self.assertEqual(events.count('memory_summary_retry'), 1)
        self.assertIn('memory_capture_failed', events)

    def test_memory_write_connection_reset_is_not_replayed(self):
        client = Mock()
        client.recall.return_value = []
        client.remember.side_effect = ConnectionResetError('write outcome unknown')
        report = {'status': 'completed', 'answer': 'Review complete',
                  'evidence': ['checked'], 'remaining_work': [], 'errors': []}
        summary = {'memories': [{'kind': 'constraint', 'text': 'Review the PR',
                                 'source': 'user', 'quote': 'Review https://github.com/Owner/Repo/pull/68'}]}
        replies = [report, summary]
        def respond(*args, **kwargs):
            content = replies.pop(0)
            return io.BytesIO(json.dumps({'choices': [{'message': {
                'content': json.dumps(content)}, 'finish_reason': 'stop'}]}).encode())
        state = {}
        with patch.dict(os.environ, ENV, clear=True), \
             patch('popot_agents.worker.turn_memory.CogneeMemory', return_value=client), \
             patch('popot_agents.worker.http_harness.mcp_client.discover_tools', return_value={}), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=respond), \
             patch('popot_agents.worker.http_harness._trace') as trace:
            self.assertEqual(run_http(TASK, ROLE, state=state), 'Review complete')
        self.assertEqual(state['outcome']['status'], 'completed')
        client.remember.assert_called_once()
        failures = [call.kwargs for call in trace.call_args_list
                    if call.args[0] == 'memory_capture_failed']
        self.assertEqual(failures[0]['phase'], 'save')

    def test_role_default_project_enables_memory_without_a_task_tag(self):
        with patch.dict(os.environ, ENV, clear=True):
            memory = TurnMemory({**ROLE,'memory_project':'popot-agents'},
                [{'role':'user','content':'Analyze local service health.'}], client=Mock())
        self.assertTrue(memory.enabled)
        self.assertEqual(memory.dataset,'popot-agents')

    def test_default_project_does_not_override_ambiguous_user_projects(self):
        with patch.dict(os.environ, ENV, clear=True):
            memory = TurnMemory({**ROLE,'memory_project':'popot-agents'},
                [{'role':'user','content':'Compare https://github.com/a/one and https://github.com/b/two'}],
                client=Mock())
        self.assertFalse(memory.enabled)

    def test_recall_keeps_provenance_when_ingested_text_is_clean(self):
        client = Mock()
        record = {'recorded_at': '2026-10-10', 'references': ['https://github.com/a/b/pull/70'],
                  'trust': 'historical; verify', 'memories': [{'text': 'Retry takes 60 seconds',
                                                            'evidence_quote': 'WAIT = 60'}]}
        client.recall.return_value = [{'text': 'Retry takes 60 seconds', 'raw': {
            'external_metadata': json.dumps({'popot_memory': {'version': 2, 'record': record}})}}]
        with patch.dict(os.environ, ENV, clear=True):
            memory = TurnMemory(ROLE, [{'role': 'user', 'content': TASK}], client=client)
        context = memory.context(60)
        for value in ('2026-10-10', 'https://github.com/a/b/pull/70', 'WAIT = 60', 'historical; verify'):
            self.assertIn(value, context)

    def test_session_records_tools_usage_and_failure_without_raw_content(self):
        client = Mock()
        with patch.dict(os.environ, {**ENV, 'HARNESS_CHAT_ID': 'a' * 16}, clear=True):
            memory = TurnMemory(ROLE, [{'role': 'user', 'content': TASK}], client=client)
            memory.start_session()
            memory.observe('call-1', 'bash', {'command': 'secret command'}, 'private output')
            memory.record_usage({'usage': {'prompt_tokens': 12, 'completion_tokens': 4, 'cost': 0.002}})
            memory.finish_session(False)
        start, finish = [c.args[0] for c in client.session_event.call_args_list]
        self.assertEqual(start['status'], 'running')
        self.assertEqual(finish['status'], 'failed')
        self.assertEqual(start['session_id'], finish['session_id'])
        self.assertIn('a' * 16, start['session_id'])
        self.assertEqual(finish['tokens_in'], 12)
        self.assertEqual(finish['cost_usd'], 0.002)
        self.assertEqual(finish['tools'][0]['name'], 'bash')
        self.assertNotIn('private output', json.dumps(finish))
        self.assertNotIn('secret command', json.dumps(finish))

    def test_journal_outage_does_not_discard_a_successful_answer(self):
        client = Mock()
        client.session_event.side_effect = RuntimeError('unavailable')
        client.recall.return_value = []
        replies = [{'choices': [{'message': {'content': 'done'}}]},
                   {'choices': [{'message': {'content': '{"memories":[]}'}}]}]
        with patch.dict(os.environ, ENV, clear=True), \
             patch('popot_agents.worker.turn_memory.CogneeMemory', return_value=client), \
             patch('popot_agents.worker.http_harness.mcp_client.discover_tools', return_value={}), \
             patch('popot_agents.worker.http_harness.request.urlopen',
                   side_effect=lambda *a, **kw: io.BytesIO(json.dumps(replies.pop(0)).encode())):
            self.assertEqual(run_http(TASK, ROLE), 'done')

    def test_summary_accepts_json_arrays_objects_and_text_content_blocks(self):
        item = {'kind': 'decision', 'text': 'Use scoped memory', 'source': 'user', 'quote': 'Review'}
        for content in (json.dumps([item]), json.dumps({'memories': [item]}),
                        '```json\n' + json.dumps([item]) + '\n```',
                        [{'type': 'text', 'text': json.dumps([item])}]):
            with self.subTest(content=type(content).__name__):
                self.assertEqual(parse_summary(content), {'memories': [item]})
        for content in (None, 'null', '123', '{}', '{"memories":"bad"}'):
            with self.subTest(content=content), self.assertRaises(ValueError):
                parse_summary(content)

    def test_empty_model_summary_is_retried_without_reexecuting_task(self):
        client = Mock()
        client.recall.return_value = []
        replies = [
            {'choices': [{'message': {'content': 'done'}}]},
            {'choices': [{'message': {'content': None}}]},
            {'choices': [{'message': {'content': json.dumps([
                {'kind': 'constraint', 'text': 'Review PR 68', 'source': 'user', 'quote': 'Review'}])}}]},
        ]
        with patch.dict(os.environ, ENV, clear=True), \
             patch('popot_agents.worker.turn_memory.CogneeMemory', return_value=client), \
             patch('popot_agents.worker.http_harness.mcp_client.discover_tools', return_value={}), \
             patch('popot_agents.worker.http_harness.request.urlopen',
                   side_effect=lambda *a, **kw: io.BytesIO(json.dumps(replies.pop(0)).encode())) as model:
            self.assertEqual(run_http(TASK, ROLE), 'done')
        self.assertEqual(model.call_count, 3)
        client.remember.assert_called_once()

    def test_scope_tracks_repo_switches_and_followups_but_rejects_ambiguity(self):
        messages = [{'role': 'user', 'content': TASK}]
        original = project_dataset(messages)
        self.assertRegex(original, r'^github-owner-repo-[0-9a-f]{16}$')
        messages.append({'role': 'user', 'content': 'Run the checks too'})
        self.assertEqual(project_dataset(messages), original)
        messages.append({'role': 'user', 'content': 'https://github.com/Else/Other.git'})
        self.assertRegex(project_dataset(messages), r'^github-else-other-[0-9a-f]{16}$')
        messages.append({'role': 'user', 'content': TASK + ' https://github.com/Else/Other'})
        self.assertIsNone(project_dataset(messages))
        self.assertIsNone(project_dataset([{'role': 'user', 'content': 'hello'}]))
        self.assertEqual(project_dataset([{'role': 'user', 'content': '[project: popot-agents] fix tests'}]), 'popot-agents')

    def test_repository_scope_is_unique_and_ignores_sentence_punctuation(self):
        def dataset(url):
            return project_dataset([{'role': 'user', 'content': f'Review {url}'}])

        dotted = dataset('https://github.com/Owner/foo.bar')
        hyphenated = dataset('https://github.com/Owner/foo-bar')
        self.assertNotEqual(dotted, hyphenated)
        self.assertEqual(dataset('https://github.com/Owner/foo.bar.'), dotted)
        self.assertEqual(dataset('https://github.com/Owner/foo.bar.git/pull/6'), dotted)
        self.assertNotEqual(dataset('https://github.com/a-b/c'),
                            dataset('https://github.com/a/b-c'))

    def test_secrets_are_removed_from_queries_and_summaries(self):
        with patch.dict(os.environ, {'GITHUB_PERSONAL_ACCESS_TOKEN': 'test-secret'}, clear=True):
            result = scrub('token test-secret password=unknown-secret ghp_abcdefghijklmnopqrstuvwx')
        for secret in ('test-secret', 'unknown-secret', 'ghp_abcdefghijklmnopqrstuvwx'):
            self.assertNotIn(secret, result)

    def test_only_supported_durable_items_are_saved_with_provenance(self):
        with patch.dict(os.environ, ENV, clear=True):
            memory = TurnMemory(ROLE, [{'role': 'user', 'content': TASK}], client=Mock())
        memory.observe('check-1', 'bash', {}, '12 tests passed')
        items = {'memories': [
            {'kind': 'check', 'text': '12 tests passed', 'source': 'check-1', 'quote': '12 tests passed'},
            {'kind': 'fix', 'text': 'Invented fix', 'source': 'missing', 'quote': 'done'},
            {'kind': 'fact', 'text': 'Invented claim', 'source': 'check-1', 'quote': 'not in output'},
            {'kind': 'chat', 'text': 'hello', 'source': 'user', 'quote': 'Review'},
        ]}
        self.assertEqual(memory.save(items), 1)
        text, dataset = memory.client.remember.call_args.args
        self.assertEqual(dataset, project_dataset([{'role': 'user', 'content': TASK}]))
        self.assertIn('12 tests passed', text)
        self.assertIn('check-1', text)
        self.assertIn('https://github.com/Owner/Repo/pull/68', text)
        self.assertNotIn('Invented', text)

    def test_saved_references_exclude_sentence_punctuation(self):
        with patch.dict(os.environ, ENV, clear=True):
            memory = TurnMemory(ROLE, [{'role': 'user', 'content': TASK + ', check it.'}], client=Mock())
        memory.observe('check', 'bash', {}, 'passed')
        memory.save({'memories': [{'kind': 'check', 'text': 'Check passed', 'source': 'check', 'quote': 'passed'}]})
        record = json.loads(memory.client.remember.call_args.args[0])
        self.assertEqual(record['references'], ['https://github.com/Owner/Repo/pull/68'])

    def test_runtime_reads_before_model_and_saves_curated_summary_after_answer(self):
        client = Mock()
        client.recall.return_value = [{'text': 'Use unittest for this repo.'}]
        replies = [
            {'choices': [{'message': {'content': 'Confirmed the requested review scope.'}}]},
            {'choices': [{'message': {'content': json.dumps({'memories': [
                {'kind': 'constraint', 'text': 'Review PR 68', 'source': 'user', 'quote': 'Review'}]})}}]},
        ]
        bodies = []
        def respond(call, **kwargs):
            bodies.append(json.loads(call.data))
            return io.BytesIO(json.dumps(replies.pop(0)).encode())
        with patch.dict(os.environ, ENV, clear=True), \
             patch('popot_agents.worker.turn_memory.CogneeMemory', return_value=client), \
             patch('popot_agents.worker.http_harness.mcp_client.discover_tools', return_value={}), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=respond):
            self.assertEqual(run_http(TASK, ROLE), 'Confirmed the requested review scope.')
        client.recall.assert_called_once()
        self.assertIn('Use unittest', json.dumps(bodies[0]['messages']))
        self.assertIn('Historical', json.dumps(bodies[0]['messages']))
        self.assertEqual(client.remember.call_count, 1)

    def test_memory_outage_does_not_fail_the_task(self):
        client = Mock()
        client.recall.side_effect = RuntimeError('private-password')
        replies = [io.BytesIO(b'{"choices":[{"message":{"content":"done"}}]}'),
                   TimeoutError('private-password')]
        with patch.dict(os.environ, ENV, clear=True), \
             patch('popot_agents.worker.turn_memory.CogneeMemory', return_value=client), \
             patch('popot_agents.worker.http_harness.mcp_client.discover_tools', return_value={}), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=replies):
            self.assertEqual(run_http(TASK, ROLE), 'done')
        client.remember.assert_not_called()

    def test_roles_without_memory_do_not_connect(self):
        with patch.dict(os.environ, ENV, clear=True), \
             patch('popot_agents.worker.turn_memory.CogneeMemory') as client:
            memory = TurnMemory({}, [{'role': 'user', 'content': TASK}])
            self.assertFalse(memory.enabled)
        client.assert_not_called()

    def test_memory_tools_cannot_switch_projects_or_bypass_curated_capture(self):
        client = Mock()
        client.recall.return_value = []
        calls = []
        replies = [
            {'choices': [{'message': {'tool_calls': [{'id': 'read-1', 'function': {
                'name': 'mcp__cognee__recall', 'arguments': '{"query":"facts","dataset_name":"other"}'}}]}}]},
            {'choices': [{'message': {'content': 'done'}}]},
            {'choices': [{'message': {'content': '{"memories":[]}'}}]},
        ]
        def respond(call, **kwargs):
            calls.append(json.loads(call.data))
            return io.BytesIO(json.dumps(replies.pop(0)).encode())
        tools = {f'mcp__cognee__{name}': {'server': 'cognee', 'native_name': name,
                 'schema': {'type': 'function', 'function': {'name': f'mcp__cognee__{name}'}}}
                 for name in ['recall', 'remember']}
        with patch.dict(os.environ, ENV, clear=True), \
             patch('popot_agents.worker.turn_memory.CogneeMemory', return_value=client), \
             patch('popot_agents.worker.http_harness.mcp_client.discover_tools', return_value=tools), \
             patch('popot_agents.worker.http_harness.mcp_client.call_tool', return_value='[]') as tool, \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=respond):
            self.assertEqual(run_http(TASK, ROLE), 'done')
        tool.assert_not_called()
        self.assertNotIn('mcp__cognee__remember', json.dumps(calls[0]['tools']))
        self.assertIn('isError', calls[1]['messages'][-1]['content'])

    def test_failed_task_never_captures_memory(self):
        client = Mock()
        client.recall.return_value = []
        with patch.dict(os.environ, ENV, clear=True), \
             patch('popot_agents.worker.turn_memory.CogneeMemory', return_value=client), \
             patch('popot_agents.worker.http_harness.mcp_client.discover_tools', return_value={}), \
             patch('popot_agents.worker.http_harness.request.urlopen', return_value=io.BytesIO(b'{}')):
            with self.assertRaisesRegex(RuntimeError, 'no answer'):
                run_http(TASK, ROLE)
        client.remember.assert_not_called()
        self.assertEqual(client.session_event.call_args.args[0]['status'], 'failed')

    def test_recall_failure_reports_redacted_cause_and_remains_optional(self):
        client = Mock()
        client.recall.side_effect = RuntimeError('HTTP 503 private-password')
        with patch.dict(os.environ, ENV, clear=True), \
             patch('popot_agents.worker.turn_memory.trace') as trace:
            memory = TurnMemory(ROLE, [{'role': 'user', 'content': TASK}], client=client)
            self.assertEqual(memory.context(10), '')
        event = next(c.kwargs for c in trace.call_args_list if c.args[0] == 'memory_recall_failed')
        self.assertIn('HTTP 503', event['error'])
        self.assertNotIn('private-password', event['error'])

    def test_optional_model_recall_failure_is_recoverable(self):
        client = Mock()
        client.recall.return_value = []
        replies = [
            {'choices': [{'message': {'tool_calls': [{'id': 'read-1', 'function': {
                'name': 'mcp__cognee__recall', 'arguments': '{"query":"facts"}'}}]}}]},
            {'choices': [{'message': {'content': 'done'}}]},
            {'choices': [{'message': {'content': '{"memories":[]}'}}]},
        ]
        tool = {'server': 'cognee', 'native_name': 'recall',
                'schema': {'type': 'function', 'function': {'name': 'mcp__cognee__recall'}}}
        with patch.dict(os.environ, ENV, clear=True), \
             patch('popot_agents.worker.turn_memory.CogneeMemory', return_value=client), \
             patch('popot_agents.worker.http_harness.mcp_client.discover_tools',
                   return_value={'mcp__cognee__recall': tool}), \
             patch('popot_agents.worker.http_harness.mcp_client.call_tool', side_effect=RuntimeError('offline')), \
             patch('popot_agents.worker.http_harness.request.urlopen',
                   side_effect=lambda *a, **kw: io.BytesIO(json.dumps(replies.pop(0)).encode())):
            self.assertEqual(run_http(TASK, ROLE), 'done')

    def test_disabled_optional_memory_does_not_block_the_main_task(self):
        for failure in ('recall', 'start', 'no_project', 'no_credentials'):
            with self.subTest(failure=failure):
                client = Mock()
                client.recall.return_value = []
                if failure == 'recall':
                    client.recall.side_effect = RuntimeError('HTTP 503')
                if failure == 'start':
                    client.session_event.side_effect = RuntimeError('HTTP 503')
                env = dict(ENV)
                if failure == 'no_credentials':
                    env.pop('COGNEE_PASSWORD')
                role = dict(ROLE)
                if failure != 'no_project':
                    role['memory_project'] = 'popot-agents'
                replies = [
                    {'tool_calls': [{'id': str(index), 'function': {
                        'name': 'mcp__cognee__recall', 'arguments': '{"query":"facts"}'}}]}
                    for index in range(RUNTIME['workflow']['max_no_progress'])]
                replies.append({'content': json.dumps({'status': 'completed', 'answer': 'done',
                    'evidence': ['Current service health inspected'], 'remaining_work': [], 'errors': []})})
                calls, state = [], {}
                def respond(request, **kwargs):
                    calls.append(json.loads(request.data))
                    return io.BytesIO(json.dumps({'choices': [{'message': replies.pop(0)}]}).encode())
                tool = {'server': 'cognee', 'native_name': 'recall',
                        'schema': {'type': 'function', 'function': {'name': 'mcp__cognee__recall'}}}
                with patch.dict(os.environ, env, clear=True), \
                     patch('popot_agents.worker.turn_memory.CogneeMemory', return_value=client), \
                     patch('popot_agents.worker.http_harness.mcp_client.discover_tools',
                           return_value={'mcp__cognee__recall': tool}), \
                     patch('popot_agents.worker.http_harness.mcp_client.call_tool') as call_tool, \
                     patch('popot_agents.worker.http_harness.request.urlopen', side_effect=respond):
                    self.assertEqual(run_http('Inspect the local services', role, state=state), 'done')
                self.assertEqual(state['outcome']['status'], 'completed')
                self.assertEqual(len(calls), RUNTIME['workflow']['max_no_progress'] + 1)
                call_tool.assert_not_called()
                errors = [json.loads(item['content'])['error'] for item in state['messages']
                          if item['role'] == 'tool']
                self.assertTrue(all('continue' in error.lower() for error in errors))
                if failure != 'no_project':
                    self.assertTrue(all('unavailable' in error for error in errors))
                    self.assertFalse(any('repository URL' in error for error in errors))
                    self.assertIn('dataset_name=popot-agents', calls[0]['messages'][0]['content'])

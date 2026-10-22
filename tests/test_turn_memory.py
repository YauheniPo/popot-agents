import io
import json
import os
import unittest
from unittest.mock import Mock, patch

from popot_agents.worker.turn_memory import TurnMemory, project_dataset, scrub, parse_summary
from popot_agents.worker.http_harness import run_http

ROLE = {'mcpServers': {'cognee': {'command': ['python', 'memory.py'],
                                 'tools': ['remember', 'recall', 'memory_status']}}}
ENV = {'HARNESS_BASE_URL': 'http://model/v1', 'HARNESS_MODEL': 'example',
       'HARNESS_TURN_TIMEOUT_SECONDS': '60', 'COGNEE_PASSWORD': 'private-password',
       'HARNESS_TOOL_ENV_NAMES_JSON': '["COGNEE_PASSWORD"]'}
TASK = 'Review https://github.com/Owner/Repo/pull/68'


class TurnMemoryTests(unittest.TestCase):
    def test_capture_keeps_model_parameters_and_waits_within_the_turn_deadline(self):
        parameters = {'reasoning': {'enabled': False}, 'chat_template_kwargs': {'enable_thinking': False},
                      'max_tokens': 8192}
        for turn_seconds, expected_timeout in ((180, 120), (60, 55)):
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
                with patch.dict(os.environ, {**ENV, 'HARNESS_TURN_TIMEOUT_SECONDS': str(turn_seconds),
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

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
        self.assertEqual(project_dataset(messages), 'github-owner-repo')
        messages.append({'role': 'user', 'content': 'Run the checks too'})
        self.assertEqual(project_dataset(messages), 'github-owner-repo')
        messages.append({'role': 'user', 'content': 'https://github.com/Else/Other.git'})
        self.assertEqual(project_dataset(messages), 'github-else-other')
        messages.append({'role': 'user', 'content': TASK + ' https://github.com/Else/Other'})
        self.assertIsNone(project_dataset(messages))
        self.assertIsNone(project_dataset([{'role': 'user', 'content': 'hello'}]))
        self.assertEqual(project_dataset([{'role': 'user', 'content': '[project: popot-agents] fix tests'}]), 'popot-agents')

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
        self.assertEqual(dataset, 'github-owner-repo')
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

"""Final-answer recovery must never repeat tools or lose their receipts."""
import json
import unittest
from unittest.mock import Mock, patch

from tests import test_agent_reliability as helpers


class FinalResultTests(unittest.TestCase):
    run_sequence = helpers.AgentReliabilityTests.run_sequence
    result = staticmethod(helpers.AgentReliabilityTests.result)
    delegate_call = staticmethod(helpers.AgentReliabilityTests.delegate_call)

    def test_final_evidence_separates_tool_results_from_model_claims(self):
        from popot_agents.worker.final_result import evidence_transcript
        messages = [
            {'role': 'user', 'content': 'Review this PR'},
            {'role': 'assistant', 'content': 'Invented after-publication read-back',
             'reasoning': 'private reasoning', 'tool_calls': [{'id': 'read', 'function': {
                 'name': 'mcp__github__pull_request_read', 'arguments': '{"method":"get"}'}}]},
            {'role': 'tool', 'tool_call_id': 'read', 'content': '{"head":"abc"}'},
            {'role': 'assistant', 'tool_calls': [{'id': 'cp', 'function': {
                'name': 'review_checkpoint', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'cp', 'content': 'Claimed all tests passed'},
        ]
        transcript = evidence_transcript(messages)
        self.assertNotIn('Invented after-publication', json.dumps(transcript))
        self.assertNotIn('private reasoning', json.dumps(transcript))
        self.assertEqual(transcript[2]['source'], 'tool_result')
        self.assertEqual(transcript[2]['tool'], 'mcp__github__pull_request_read')
        self.assertEqual(transcript[-1]['source'], 'model_reported_progress')

    def test_format_repair_does_not_treat_invalid_answer_as_evidence(self):
        _, _, bodies = self.run_sequence([
            {'tool_calls': [{'id': 'calc', 'function': {'name': 'calculate',
                'arguments': '{"expression":"2+2"}'}}]},
            {'content': 'Unverified claim: published 12 comments'}, self.result()])
        transcript = bodies[-1]['messages'][-1]['content']
        self.assertNotIn('Unverified claim', transcript)
        self.assertIn('tool_result', transcript)

    def test_final_evidence_preserves_order_and_historical_child_attribution(self):
        from popot_agents.worker.final_result import evidence_transcript
        messages = []
        for identifier, name, result in [
                ('memory', 'mcp__cognee__recall', 'old CI success'),
                ('before', 'mcp__github__pull_request_read', '12 threads'),
                ('write', 'mcp__github__pull_request_review_write', 'published'),
                ('head', 'mcp__github__pull_request_read', 'head abc'),
                ('child', 'delegate_task', 'child reported success')]:
            messages.extend([{'role': 'assistant', 'tool_calls': [{'id': identifier,
                'function': {'name': name, 'arguments': '{}'}}]},
                {'role': 'tool', 'tool_call_id': identifier, 'content': result}])
        receipts = [item for item in evidence_transcript(messages) if 'call_id' in item]
        self.assertEqual([item['call_id'] for item in receipts],
                         ['memory', 'before', 'write', 'head', 'child'])
        self.assertEqual(receipts[0]['source'], 'historical_memory')
        self.assertEqual(receipts[-1]['source'], 'delegated_report')
        self.assertEqual(receipts[3]['content'], 'head abc')

    def test_original_assistant_context_is_kept_as_context_not_current_proof(self):
        from popot_agents.worker.final_result import evidence_transcript
        previous = {'role': 'assistant', 'content': 'Original draft to translate'}
        transcript = evidence_transcript([previous, {'role': 'user', 'content': 'Translate it'},
            {'role': 'assistant', 'content': 'Unsupported current claim'}], context=[previous])
        self.assertEqual(transcript[0]['source'], 'scope_or_context_not_execution_evidence')
        self.assertEqual(transcript[0]['content'], previous['content'])
        self.assertNotIn('Unsupported current claim', json.dumps(transcript))

    def test_fallback_receipts_do_not_treat_checkpoints_as_observed_results(self):
        from popot_agents.worker.final_result import retained_receipts
        receipts = retained_receipts([{'role': 'assistant', 'tool_calls': [{
            'id': 'checkpoint', 'function': {'name': 'review_checkpoint'}}]},
            {'role': 'tool', 'tool_call_id': 'checkpoint', 'content': 'Claimed CI passed'}], {})
        self.assertEqual(receipts, [])

    def test_failed_format_repair_preserves_completed_child_evidence(self):
        url = 'https://github.com/example/repo/pull/80#pullrequestreview-123'
        delegate = Mock(return_value=json.dumps({'sessionId': 'a'*16, 'role': 'qa_engineer',
            'state': 'TASK_STATE_COMPLETED', 'answer': 'Published and read back: ' + url,
            'outcome': {'status': 'completed', 'evidence': [url], 'remaining_work': [], 'errors': []}}))
        answer, state, bodies = self.run_sequence([self.delegate_call(),
            {'content': 'Published: ' + url}, {'content': 'invalid again'}], delegate=delegate)
        delegate.assert_called_once()
        self.assertEqual(len(bodies), 3)
        self.assertNotIn('tools', bodies[-1])
        self.assertIn(url, answer)
        self.assertIn(url, '\n'.join(state['outcome']['evidence']))
        self.assertEqual(state['outcome']['status'], 'partial')
        self.assertTrue(any(m.get('content') == 'invalid again' for m in state['messages']))

    def test_empty_content_with_reasoning_gets_one_tool_free_repair(self):
        answer, state, bodies = self.run_sequence([
            {'content': '', 'reasoning': 'I should return a JSON result'}, self.result()])
        self.assertEqual(state['outcome']['status'], 'completed')
        self.assertEqual(len(bodies), 2)
        self.assertNotIn('tools', bodies[-1])

    def test_invalid_forced_final_also_gets_one_format_repair(self):
        clock = [0]
        def first():
            clock[0] = 780
            return {'tool_calls': [{'id': 'calc', 'function': {'name': 'calculate',
                'arguments': '{"expression":"2+2"}'}}]}
        _, state, bodies = self.run_sequence([first, {'content': 'not JSON'}, self.result()], clock=clock)
        self.assertEqual(state['outcome']['status'], 'completed')
        self.assertTrue(all(b.get('tool_choice') == 'none' for b in bodies[1:]))

    def test_invalid_results_keep_tool_receipts_without_claiming_completion(self):
        call = {'tool_calls': [{'id': 'calc', 'function': {'name': 'calculate',
            'arguments': '{"expression":"2+2"}'}}]}
        answer, state, _ = self.run_sequence([call, {'content': 'bad'}, {'content': 'bad again'}])
        self.assertEqual(state['outcome']['status'], 'partial')
        self.assertIn('calculate', '\n'.join(state['outcome']['evidence']))
        self.assertIn('4', answer)

    def test_final_response_diagnostics_identify_schema_error_without_content(self):
        with patch('popot_agents.worker.http_harness._trace') as trace:
            _, state, _ = self.run_sequence([{'content': '{}'}, {'content': 'bad'}])
        events = [c.kwargs for c in trace.call_args_list if c.args[0] == 'final_result_invalid']
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]['reason'], 'invalid_status')
        self.assertEqual(events[1]['reason'], 'invalid_json')
        self.assertNotIn('content', events[0])
        self.assertIn('invalid_json', state['outcome']['errors'][0])

    def test_fenced_json_is_parsed_without_another_request(self):
        result = self.result()['content']
        _, state, bodies = self.run_sequence([{'content': '```json\n' + result + '\n```'}])
        self.assertEqual(state['outcome']['status'], 'completed')
        self.assertEqual(len(bodies), 1)

    def test_cloud_format_repair_disables_thinking_without_changing_work_requests(self):
        parameters = {'reasoning': {'enabled': True}}
        _, _, bodies = self.run_sequence([{'content': 'bad'}, self.result()], env={
            'HARNESS_UPSTREAM_HOST': 'ollama.com',
            'HARNESS_MODEL_PARAMETERS_JSON': json.dumps(parameters)})
        self.assertEqual(bodies[0]['reasoning'], {'enabled': True})
        self.assertEqual(bodies[-1]['reasoning_effort'], 'none')
        self.assertNotIn('reasoning', bodies[-1])
        self.assertNotIn('response_format', bodies[-1])
        self.assertEqual(bodies[-1]['temperature'], 0)

    def test_format_repair_timeout_preserves_receipt_and_does_not_retry_work(self):
        def timeout():
            raise TimeoutError('provider timeout')
        call = {'tool_calls': [{'id': 'calc', 'function': {'name': 'calculate',
            'arguments': '{"expression":"2+2"}'}}]}
        answer, state, bodies = self.run_sequence([call, {'content': 'bad'}, timeout])
        self.assertEqual(len(bodies), 3)
        self.assertEqual(state['outcome']['status'], 'partial')
        self.assertIn('calculate', answer)

    def test_parser_rejects_truncation_even_when_content_is_valid_json(self):
        from popot_agents.worker.final_result import parse_result
        with self.assertRaisesRegex(ValueError, 'truncated_result'):
            parse_result(self.result()['content'], 'length')
        for content in ('[]', '{"status": "completed"}', 'prefix ' + self.result()['content']):
            with self.subTest(content=content), self.assertRaises(ValueError):
                parse_result(content)

    def test_retained_receipts_redact_credentials_and_keep_errors_as_receipts(self):
        from popot_agents.worker.final_result import retained_receipts
        with patch.dict('os.environ', {'GITHUB_PERSONAL_ACCESS_TOKEN': 'secret-test-value'}):
            receipts = retained_receipts([{'role': 'assistant', 'tool_calls': [
                {'id': 'write', 'function': {'name': 'mcp__github__write'}}]},
                {'role': 'tool', 'tool_call_id': 'write',
                 'content': '{"isError":true,"error":"secret-test-value"}'}], {})
        self.assertNotIn('secret-test-value', '\n'.join(receipts))
        self.assertIn('isError', receipts[0])
        self.assertIn('outcome requires interpretation', receipts[0])

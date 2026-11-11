"""Final-answer recovery must never repeat tools or lose their receipts."""
import json
import unittest
from unittest.mock import Mock, patch

from tests import test_agent_reliability as helpers


class FinalResultTests(unittest.TestCase):
    run_sequence = helpers.AgentReliabilityTests.run_sequence
    result = staticmethod(helpers.AgentReliabilityTests.result)
    delegate_call = staticmethod(helpers.AgentReliabilityTests.delegate_call)

    def test_findings_error_identifies_the_invalid_field_without_echoing_its_value(self):
        from popot_agents.worker.final_result import parse_result
        valid = {'description': 'Observed limitation', 'scope': 'out_of_scope',
                 'status': 'open', 'rationale': 'Present before this change'}
        cases = [
            ({}, 'findings must be a list'),
            (['PRIVATE_VALUE'], 'findings[0] must be an object'),
            ([{**valid, 'description': None}], 'findings[0].description must be a nonempty string'),
            ([{**valid, 'scope': 'PRIVATE_VALUE'}], 'findings[0].scope must be in_scope or out_of_scope'),
            ([valid, {**valid, 'status': []}], 'findings[1].status must be open or resolved'),
            ([{**valid, 'rationale': ''}],
             'findings[0].rationale must be a nonempty string for out_of_scope'),
        ]
        for findings, detail in cases:
            with self.subTest(detail=detail):
                report = {**json.loads(self.result()['content']), 'findings': findings}
                with self.assertRaises(ValueError) as caught:
                    parse_result(json.dumps(report))
                self.assertEqual(str(caught.exception), 'invalid_findings: ' + detail)
                self.assertNotIn('PRIVATE_VALUE', str(caught.exception))

    def test_findings_repair_receives_field_guidance_and_preserves_valid_findings(self):
        finding = {'description': 'Existing limitation', 'scope': 'out_of_scope',
                   'status': 'open', 'rationale': 'Outside the assigned change'}
        valid = {**json.loads(self.result()['content']), 'findings': [finding]}
        invalid = {**valid, 'findings': [{**finding, 'scope': 'out-of-scope'}]}
        with patch('popot_agents.worker.http_harness._trace') as trace:
            _, state, bodies = self.run_sequence([
                {'content': json.dumps(invalid)}, {'content': json.dumps(valid)}])
        self.assertEqual(len(bodies), 2)
        self.assertNotIn('tools', bodies[-1])
        self.assertIn('findings[0].scope must be in_scope or out_of_scope',
                      bodies[-1]['messages'][0]['content'])
        self.assertEqual(state['outcome']['status'], 'completed')
        self.assertEqual(state['outcome']['findings'], [finding])
        failure = next(call.kwargs for call in trace.call_args_list
                       if call.args[0] == 'final_result_invalid')
        self.assertEqual(failure['reason'], 'invalid_findings')
        self.assertEqual(failure['detail'], 'findings[0].scope must be in_scope or out_of_scope')

    def test_repaired_format_does_not_mask_an_unresolved_child_failure(self):
        state = {'messages': [{'role': 'user', 'content': 'Verify the requested work'}],
                 'outcome': {'status': 'partial'}, 'failed_delegations': [[
                     ['qa_engineer', 'Verify the artifact'], {'role': 'qa_engineer', 'session_id': None,
                     'status': 'incomplete', 'resumable': False, 'pending': False,
                     'error': 'Submission failed', 'outcome': {}}]]}
        answer, state, bodies = self.run_sequence([{'content': 'bad JSON'}, self.result()], state=state)
        self.assertEqual(len(bodies), 2)
        self.assertEqual(bodies[-1]['tool_choice'], 'none')
        self.assertEqual(state['outcome']['reason'], 'delegated_task_incomplete')
        self.assertNotIn('invalid final result', answer)
        self.assertEqual(state['outcome']['errors'], ['Submission failed'])

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

    def test_format_repair_is_request_scoped_when_required_verification_remains(self):
        from popot_agents.prompts import render_prompt
        partial = self.result('partial', remaining=['Obtain independent QA at the target revision'])
        _, state, bodies = self.run_sequence([{'content': 'bad JSON'}, partial])
        repair_prefix = render_prompt('reporting/repair', reason='invalid_json', result_contract='').splitlines()[0]
        self.assertEqual(bodies[-1]['messages'][0]['content'].splitlines()[0], repair_prefix)
        self.assertNotIn('tools', bodies[-1])
        self.assertEqual(state['outcome']['status'], 'partial')
        self.assertFalse(any(m.get('role') == 'system' and repair_prefix in m.get('content', '')
                             for m in state['messages']))
        delegate = Mock(return_value=json.dumps({'sessionId': 'a'*16, 'role': 'qa_engineer',
            'state': 'TASK_STATE_COMPLETED', 'answer': 'QA assessed', 'outcome': {
                'status': 'completed', 'evidence': ['QA check'], 'errors': [], 'remaining_work': []}}))
        _, resumed, requests = self.run_sequence([self.delegate_call(), self.result()], state=state, delegate=delegate)
        delegate.assert_called_once()
        self.assertIn('tools', requests[0])
        self.assertFalse(any(m.get('role') == 'system' and repair_prefix in m.get('content', '')
                             for m in requests[0]['messages']))
        self.assertEqual(resumed['outcome']['status'], 'completed')

    def test_legacy_checkpoint_drops_only_runtime_format_repair_directives(self):
        from popot_agents.prompts import render_prompt
        repair = render_prompt('reporting/repair', reason='invalid_json', result_contract='OLD CONTRACT')
        context = {'role': 'system', 'content': 'Keep independent QA and the original deadline'}
        quoted = {'role': 'user', 'content': repair}
        state = {'messages': [context, quoted, {'role': 'system', 'content': repair}],
                 'outcome': {'status': 'partial'}}
        _, _, bodies = self.run_sequence([self.result('partial', remaining=['QA'])], state=state)
        messages = bodies[0]['messages']
        self.assertIn(context, messages)
        self.assertIn(quoted, messages)
        self.assertFalse(any(m.get('role') == 'system' and m.get('content') == repair for m in messages),
                         'Legacy runtime repair directive must not govern a new work turn')

    def test_format_repair_does_not_bypass_missing_independent_verification(self):
        child = {'sessionId': 'a'*16, 'role': 'backend_engineer', 'outcome': {
            'status': 'completed', 'revision': 'b'*40, 'evidence': ['implementation observed'],
            'errors': [], 'remaining_work': []}}
        state = {'messages': [{'role': 'user', 'content': 'Verify the implementation'}],
                 'outcome': {'status': 'partial'}, 'completed_delegations': {'a'*16: child}}
        _, state, bodies = self.run_sequence([{'content': 'bad JSON'}, self.result()], state=state,
            role={'name': 'planner', '_workflow_coordinator': True, 'tools': []})
        self.assertEqual(state['outcome']['reason'], 'workflow_validation_missing')
        self.assertEqual(state['outcome']['status'], 'partial')
        self.assertIn('code_reviewer, qa_engineer', ' '.join(state['outcome']['remaining_work']))
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

    def test_multiline_report_is_normalized_without_a_model_repair(self):
        answer = 'Проверено:\n| check | result |\n| tests | PASS |\r\n\tExit 0'
        report = self.result(answer=answer)['content']
        report = report.replace('\\n', '\n').replace('\\r', '\r').replace('\\t', '\t')
        with patch('popot_agents.worker.http_harness._trace') as trace:
            actual, state, bodies = self.run_sequence([{'content': report}, self.result(answer=answer)])
        self.assertEqual(actual, answer)
        self.assertEqual(state['outcome']['status'], 'completed')
        self.assertEqual(len(bodies), 1, 'Formatting alone must not spend another model request')
        self.assertFalse(any(c.args[0] == 'final_result_invalid' for c in trace.call_args_list))

    def test_trailing_separators_preserve_report_data_without_a_model_repair(self):
        from popot_agents.worker.final_result import parse_result
        report = {'status': 'partial', 'answer': 'Keep ,} and ,] and \\"quoted\\" text',
                  'evidence': ['stdout: ok'], 'remaining_work': ['QA'], 'errors': [],
                  'findings': [{'description': 'verify', 'scope': 'in_scope', 'status': 'open'}]}
        text = json.dumps(report, ensure_ascii=False).replace('"stdout: ok"]', '"stdout: ok",]')
        text = text.replace('"status": "open"}', '"status": "open",}').removesuffix('}') + ',}'
        self.assertEqual(parse_result(text), report)

    def test_transport_wrappers_accept_bom_and_crlf_without_extracting_prose(self):
        from popot_agents.worker.final_result import parse_result
        report = self.result()['content']
        for text in ('\ufeff' + report, '```json\r\n' + report + '\r\n```',
                     '```JSON \n' + report + '\n```'):
            with self.subTest(text=text[:12]):
                self.assertEqual(parse_result(text), json.loads(report))

    def test_normalization_does_not_guess_missing_data_or_accept_invalid_schema(self):
        from popot_agents.worker.final_result import parse_result
        valid = self.result()['content']
        for text in (valid[:-1], valid + valid, 'prefix ' + valid, valid + '\nDone',
                     valid.replace('Result verified', 'broken "quote'),
                     valid.replace('Result verified', 'bad\\q'),
                     valid.replace('Result verified', 'bad\x00control'),
                     valid.replace('"errors": []', '"errors": [,]'),
                     valid.replace('"errors": []', '"errors": [1,,]'),
                     valid.replace('"errors": []', '"errors": {,}'),
                     '{"status":"completed","answer":"text",}',
                     valid.replace('"errors": []', '"errors": "none"')):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_result(text)
        with self.assertRaisesRegex(ValueError, 'truncated_result'):
            parse_result(valid.replace('Result verified', 'line\nbreak'), 'length')

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
        from popot_agents.runtime_config import RUNTIME
        with patch.dict(RUNTIME['model'], {'request_retries': 0}):
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

    def test_retained_tool_receipt_is_short_and_links_full_output(self):
        from popot_agents.worker.final_result import retained_receipts
        artifact='.tool-results/'+'a'*32+'.txt'
        content='exit_code=0\n'+'long output\n'*300+f'Full output: {artifact}\n'
        receipts=retained_receipts([{'role':'assistant','tool_calls':[
            {'id':'shell','function':{'name':'bash'}}]},
            {'role':'tool','tool_call_id':'shell','content':content}],{})
        self.assertEqual(len(receipts),1)
        self.assertLess(len(receipts[0]),500)
        self.assertIn('exit_code=0',receipts[0])
        self.assertIn(artifact,receipts[0])

    def test_blank_child_and_tool_entries_are_skipped_and_own_tools_are_not_receipts(self):
        from popot_agents.worker.final_result import retained_receipts
        children = {'s': {'role': 'backend_engineer', 'sessionId': 'b'*16, 'answer': 'tests green',
                          'outcome': {'evidence': [None, '   ', 'exit code 0']}}}
        messages = [{'role': 'assistant', 'tool_calls': [
            {'id': 'checkpoint', 'function': {'name': 'review_checkpoint'}},
            {'id': 'read', 'function': {'name': 'read_file'}}]},
            {'role': 'tool', 'tool_call_id': 'checkpoint', 'content': 'claimed review passed'},
            {'role': 'tool', 'tool_call_id': 'read', 'content': '   '},
            {'role': 'tool', 'tool_call_id': 'lost', 'content': 'orphan stdout'}]
        receipts = retained_receipts(messages, children)
        joined = '\n'.join(receipts)
        self.assertIn('exit code 0', joined)
        self.assertIn('Tool receipt tool (lost)', joined)
        self.assertNotIn('claimed review passed', joined)
        self.assertEqual(len(receipts), 3)

    def test_child_receipts_preserve_order_deduplication_and_limit_after_redaction(self):
        from popot_agents.worker.final_result import retained_receipts
        from popot_agents.runtime_config import RUNTIME
        children = {
            'first': {'role': 'backend_engineer', 'sessionId': 'a'*16, 'answer': 'first secret-test-value',
                      'outcome': {'evidence': ['first secret-test-value', '', None, 'check passed']}},
            'second': {'role': 'qa_engineer', 'sessionId': 'b'*16, 'answer': 'second',
                       'outcome': {'evidence': ['must be truncated']}},
        }
        with patch.dict(RUNTIME['memory'], {'tool_evidence_count': 3, 'tool_evidence_chars': 100}), \
             patch.dict('os.environ', {'GITHUB_PERSONAL_ACCESS_TOKEN': 'secret-test-value'}):
            receipts = retained_receipts([], children)
        first = f"Child backend_engineer (sessionId={'a'*16}) reported: "
        second = f"Child qa_engineer (sessionId={'b'*16}) reported: "
        self.assertEqual(receipts, [first + 'first [REDACTED]', first + 'check passed', second + 'second'])

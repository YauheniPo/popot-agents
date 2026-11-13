"""Capture actual harness IO without changing execution or charging tokens twice."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class TraceCaptureTests(unittest.TestCase):
    def test_payload_and_digest_use_original_credentials_and_echoes(self):
        import hashlib
        from ax_local.api.phoenix import Span
        request_body = {'password': 'fixture-secret', 'messages': [
            {'role': 'user', 'content': '{"token":"fixture-token"}'}]}
        with patch.dict(os.environ, {'TEST_API_KEY': 'fixture-secret'}):
            span = Span(Mock(), 'model', 'LLM')
            span.model_input(json.dumps(request_body).encode())
            span.model_result(200, b'{"choices":[{"message":{"content":"fixture-secret"}}]}')
        attrs = span.data['attributes']
        self.assertEqual(json.loads(attrs['input.value']), request_body)
        self.assertIn('fixture-secret', attrs['output.value'])
        encoded = json.dumps(request_body, ensure_ascii=False, separators=(',', ':')).encode()
        self.assertEqual(attrs['popot.input.sha256'], hashlib.sha256(encoded).hexdigest())

    def test_eval_child_startup_failure_uses_saved_state_and_stays_distinct_from_recovery(self):
        from evals.phoenix import build_spans
        row = {'case_id': 'delegation', 'repetition': 1, 'session_id': 'root',
            'started_at_ns': 1, 'finished_at_ns': 100, 'passed': True,
            'result': {'saved_chat': {'work': {'tasks': [
                {'sessionId': 'child', 'executionState': 'TASK_STATE_FAILED',
                 'outcome': {'status': 'failed', 'reason': 'Actor could not start'},
                 'failure': {'code': 'actor_resume_failed', 'stage': 'worker_startup'}}]}}}}
        events = [{'event': 'preparing_workspace', 'agent_session_id': 'child',
                   'parent_session_id': 'root', 'observed_at_ns': 2}]
        spans = build_spans(row, events, 'experiment')
        child = next(s for s in spans if s['kind'] == 'AGENT')
        self.assertTrue(child['error'])
        self.assertEqual(child['attributes']['popot.failure.code'], 'actor_resume_failed')
        self.assertIn('Actor could not start', child['attributes']['output.value'])
        self.assertFalse(spans[0]['error'])

    def test_upstream_error_is_visible_only_in_trace_and_preserves_http_contract(self):
        import io
        from urllib.error import HTTPError
        from ax_local.api.provider_proxy import dispatch_request, Provider
        from ax_local.api.phoenix import Span
        span = Span(Mock(), 'model', 'LLM')
        error = HTTPError('http://unused', 429, 'limited', {}, io.BytesIO(b'{"error":"quota","apiKey":"hidden-value"}'))
        with patch('ax_local.api.provider_proxy.request.urlopen', side_effect=error):
            status, body = dispatch_request({'key': Provider('test', 'fixed', 'http://unused', None)},
                '/v1/chat/completions', 'Bearer key', b'{"model":"fixed"}', error_observer=span.upstream_error)
        self.assertEqual((status, body), (429, b'{}'))
        self.assertIn('quota', span.data['attributes']['error.value'])
        self.assertIn('hidden-value', json.dumps(span.data))

    def test_tool_rejection_retains_actual_error_result(self):
        from ax_local.api.phoenix import AgentSpan
        root = AgentSpan(Mock(), 'a' * 16, 'test', {}, None)
        root.event(json.dumps({'event': 'tool_error', 'call_id': 'bad', 'name': 'calculate',
            'input': {'expression': 'bad'}, 'error_type': 'ToolInputError'}))
        root.event(json.dumps({'event': 'tool_result', 'call_id': 'bad', 'output': '{"isError":true,"error":"invalid expression"}'}))
        tool = root.tracer.submit.call_args.args[0]
        self.assertEqual(tool['status_code'], 'ERROR')
        self.assertIn('invalid expression', tool['attributes']['output.value'])
        self.assertTrue(tool['attributes']['popot.call_started_missing'])

    def test_proxy_capture_failure_preserves_the_provider_response(self):
        from ax_local.api.provider_proxy import Provider, start_proxy
        from urllib import request
        tracer = Mock()
        tracer.model_span.return_value.model_input.side_effect = ValueError('capture failure')
        tracer.model_span.return_value.model_result.side_effect = ValueError('capture failure')
        proxy = start_proxy({'key': Provider('test', 'fixed', 'http://unused', None)},
                            host='127.0.0.1', port=0, tracer=tracer)
        try:
            with patch('ax_local.api.provider_proxy.dispatch_request', return_value=(200, b'{"choices":[]}')):
                call = request.Request(f'http://127.0.0.1:{proxy.server_port}/v1/chat/completions',
                    data=b'{"model":"fixed"}', headers={'Authorization': 'Bearer key'})
                with request.urlopen(call, timeout=3) as response:
                    self.assertEqual(response.read(), b'{"choices":[]}')
        finally:
            proxy.shutdown()
            proxy.server_close()

    def test_metadata_audit_counts_chunks_and_missing_content_without_copying_bodies(self):
        from evals.runtime import build_report, sanitize_span
        raw = {'context': {'span_id': 'x'}, 'span_kind': 'CHAIN', 'name': 'Harness events',
            'status_code': 'OK', 'parent_id': None, 'start_time': '2026-11-13T10:00:00Z',
            'end_time': '2026-11-13T10:00:01Z', 'events': [{'name': 'context_compacted'}],
            'attributes': {'popot.harness.events_chunk': True, 'input.value': 'private content',
                           'popot.telemetry.version': 'popot-telemetry-v2'}}
        cleaned = sanitize_span(raw)
        self.assertNotIn('private content', json.dumps(cleaned))
        report = build_report([cleaned], {})
        self.assertEqual(report['audit']['harness_event_chunks'], 1)
        self.assertEqual(report['summary']['retained_event_counts']['context_compacted'], 1)

    def test_nested_json_credentials_and_echoes_are_preserved(self):
        from ax_local.api.phoenix import Span
        span = Span(Mock(), 'fixed', 'LLM')
        span.model_input(json.dumps({'messages': [{'role': 'tool', 'content': '{"apiKey":"hidden-value"}'}]}).encode())
        span.model_result(200, b'{"choices":[{"message":{"role":"assistant","content":"hidden-value"}}]}')
        self.assertIn('hidden-value', json.dumps(span.data))

    def test_payloads_are_valid_bounded_json_with_original_values(self):
        from popot_agents.telemetry import payload_attributes
        with patch.dict(os.environ, {'TEST_API_KEY': 'private-value'}):
            value = {'text': 'hello private-value', 'accessToken': 'another-private-value', 'number': 1234}
            attrs = payload_attributes('input', value)
            self.assertEqual(json.loads(attrs['input.value']), value)
            self.assertEqual(json.loads(attrs['input.value'])['number'], 1234)
            self.assertFalse(attrs['popot.input.truncated'])
            large = payload_attributes('input', {'text': 'я' * 10000}, limit=1024)
        self.assertTrue(large['popot.input.truncated'])
        self.assertLessEqual(len(large['input.value'].encode()), 1024)
        self.assertTrue(json.loads(large['input.value'])['truncated'])

    def test_runtime_model_span_has_request_messages_response_tools_and_usage(self):
        from ax_local.api.phoenix import Span
        tracer = Mock()
        span = Span(tracer, 'model', 'LLM')
        request = {'model': 'fixed', 'messages': [{'role': 'system', 'content': 'rules'},
                    {'role': 'user', 'content': 'calculate'}], 'max_tokens': 100,
                   'tools': [{'type': 'function', 'function': {'name': 'calculate'}}],
                   'password': 'private-input'}
        span.model_input(json.dumps(request).encode(), request_id='a' * 32)
        response = {'model': 'fixed', 'choices': [{'finish_reason': 'tool_calls', 'message': {
            'role': 'assistant', 'tool_calls': [{'id': 'c', 'type': 'function',
                'function': {'name': 'calculate', 'arguments': '{"expression":"6*7"}'}}]}}],
            'usage': {'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10}}
        span.model_result(200, json.dumps(response).encode())
        attrs = tracer.submit.call_args.args[0]['attributes']
        self.assertEqual(json.loads(attrs['input.value'])['messages'][0]['content'], 'rules')
        self.assertIn('calculate', attrs['output.value'])
        self.assertEqual(attrs['llm.input_messages.0.message.role'], 'system')
        self.assertEqual(attrs['llm.token_count.total'], 10)
        self.assertEqual(attrs['popot.request_id'], 'a' * 32)
        self.assertIn('private-input', json.dumps(attrs))

    def test_tool_io_control_chunks_and_skills_retain_original_values(self):
        from ax_local.api.phoenix import AgentSpan
        tracer = Mock()
        root = AgentSpan(tracer, 'a' * 16, 'backend_engineer', {'skills': ['engineering/tdd']}, None)
        root.event(json.dumps({'event': 'skills_loaded', 'skills': ['engineering/tdd'], 'prompt_sha256': 'b' * 64}))
        root.event(json.dumps({'event': 'tool_call', 'call_id': 't', 'name': 'bash',
                               'input': {'command': 'pytest -q', 'password': 'private-input'}}))
        root.event(json.dumps({'event': 'tool_result', 'call_id': 't', 'name': 'bash',
                               'output': '2 passed; private-input'}))
        for i in range(200):
            root.event(json.dumps({'event': 'context_compacted', 'sequence': i, 'before_chars': 100, 'after_chars': 50}))
        root.result({'answer': 'finished', 'outcome': {'status': 'completed'}})
        root.finish()
        spans = [call.args[0] for call in tracer.submit.call_args_list]
        tool = next(s for s in spans if s['span_kind'] == 'TOOL')
        self.assertIn('pytest -q', tool['attributes']['input.value'])
        self.assertIn('2 passed', tool['attributes']['output.value'])
        self.assertIn('private-input', json.dumps(spans))
        compact = [e for s in spans for e in s.get('events', []) if e['name'] == 'context_compacted']
        self.assertEqual(len(compact), 200)
        self.assertEqual(root.data['attributes']['popot.skills.loaded'], ['engineering/tdd'])
        self.assertEqual(root.data['attributes']['popot.skills.prompt_sha256'], 'b' * 64)
        self.assertIn('finished', root.data['attributes']['output.value'])

    def test_eval_uses_recorded_payload_with_root_ownership_and_no_duplicate_tokens(self):
        from evals.phoenix import build_spans
        sid, child, rid = 'a' * 16, 'b' * 16, 'c' * 32
        row = {'case_id': 'case', 'repetition': 1, 'session_id': sid, 'started_at_ns': 1,
               'finished_at_ns': 30, 'passed': True, 'outcome': {'status': 'completed'},
               'gateway_calls': [{'id': 'g', 'session_id': child, 'request_id': rid,
                    'started_at': '1970-01-01T00:00:00Z', 'duration_ms': .00002,
                    'model': 'fixed', 'tokens': 10, 'usage': {'prompt_tokens': 7, 'completion_tokens': 3}}]}
        events = [{'event': 'skills_loaded', 'agent_session_id': child, 'parent_session_id': sid,
                   'skills': ['engineering/tdd'], 'observed_at_ns': 1},
                  {'event': 'model_request', 'request_id': rid, 'agent_session_id': child, 'observed_at_ns': 2},
                  {'event': 'model_response', 'request_id': rid, 'agent_session_id': child, 'observed_at_ns': 20}]
        records = [{'context': {'span_id': 'd' * 16, 'trace_id': 'e' * 32}, 'name': 'fixed', 'span_kind': 'LLM',
                    'start_time': '1970-01-01T00:00:00Z', 'end_time': '1970-01-01T00:00:00.000020Z',
                    'status_code': 'OK', 'attributes': {'popot.agent_session_id': child, 'popot.request_id': rid,
                        'input.value': '{"messages":[{"role":"user","content":"task"}]}',
                        'output.value': '{"choices":[]}', 'llm.token_count.total': 10}}]
        spans = build_spans(row, events, 'experiment', model_records=records)
        models = [s for s in spans if s['kind'] == 'LLM']
        self.assertEqual(len(models), 1)
        self.assertEqual(sum(s['attributes'].get('llm.token_count.total', 0) for s in models), 10)
        self.assertIn('task', models[0]['attributes']['input.value'])
        self.assertEqual({s['attributes']['session.id'] for s in spans}, {sid})
        agent = next(s for s in spans if s['kind'] == 'AGENT')
        self.assertEqual(agent['attributes']['popot.skills.loaded'], ['engineering/tdd'])

    def test_parallel_eval_model_records_remain_separate(self):
        from concurrent.futures import ThreadPoolExecutor
        from evals.baseline import ModelCapture
        with tempfile.TemporaryDirectory() as tmp:
            recorder = ModelCapture(Path(tmp) / 'model-spans.jsonl')
            def capture(index):
                span = recorder.model_span(str(index) * 16, 'fixed')
                span.model_input(json.dumps({'messages': [{'role': 'user', 'content': str(index)}]}).encode(),
                                 request_id=str(index) * 32)
                span.model_result(200, b'{"choices": []}')
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(capture, (1, 2)))
            records = [json.loads(line) for line in recorder.path.read_text().splitlines()]
            self.assertEqual(len(records), 2)
            self.assertEqual({r['attributes']['popot.request_id'] for r in records}, {'1' * 32, '2' * 32})
            self.assertEqual(recorder.path.stat().st_mode & 0o777, 0o600)

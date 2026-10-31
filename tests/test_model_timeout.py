import io
import json
import os
import unittest
from contextlib import redirect_stderr
from urllib.error import HTTPError, URLError
from unittest.mock import Mock, patch

from popot_agents.worker.http_harness import run_http
from popot_agents.runtime_config import RUNTIME


class ModelTimeoutTests(unittest.TestCase):
    def test_transient_work_and_final_failures_keep_tool_results_and_checkpoint(self):
        for failure in (TimeoutError(), HTTPError('http://model', 503, 'unavailable', {}, io.BytesIO())):
            with self.subTest(failure=type(failure).__name__):
                state = {}
                first = {'choices': [{'message': {'tool_calls': [
                    {'id': 'write-proof', 'function': {'name': 'write_file',
                     'arguments': '{"path":"proof.txt","content":"saved"}'}}]}}]}
                with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://model/v1', 'HARNESS_MODEL': 'example'}, clear=True), \
                     patch.dict(RUNTIME['model'], {'request_retries': 0}), \
                     patch('popot_agents.worker.http_harness.request.urlopen', side_effect=[
                         io.BytesIO(json.dumps(first).encode()), failure, failure]) as model, \
                     patch('popot_agents.worker.http_harness.execute_tool', return_value='saved') as tool, \
                     redirect_stderr(io.StringIO()):
                    answer = run_http('Write then verify', {'tools': ['write_file']}, state=state)
                self.assertIn('model request failed', answer)
                self.assertEqual(state['outcome']['status'], 'partial')
                self.assertEqual(state['outcome']['reason'], 'model_request_failed')
                self.assertTrue(state['outcome']['errors'])
                self.assertTrue(any(item.get('tool_call_id') == 'write-proof' for item in state['messages']))
                self.assertEqual(model.call_count, 3)
                self.assertEqual(json.loads(model.call_args.args[0].data)['tool_choice'], 'none')
                tool.assert_called_once()

    def test_exhausted_work_request_timeout_still_saves_a_partial_checkpoint(self):
        now, bodies, state = [0.0], [], {}
        def respond(call, timeout):
            body = json.loads(call.data)
            bodies.append(body)
            if len(bodies) == 1:
                return io.BytesIO(json.dumps({'choices': [{'message': {'tool_calls': [
                    {'id': 'write-proof', 'function': {'name': 'write_file',
                     'arguments': '{"path":"proof.txt","content":"saved"}'}}]}}]}).encode())
            if len(bodies) == 2:
                # Socket timeouts can return just before the work deadline.
                now[0] = 39.999
                raise TimeoutError()
            self.assertEqual(body['tool_choice'], 'none')
            return io.BytesIO(json.dumps({'choices': [{'message': {'content': json.dumps({
                'status': 'partial', 'answer': 'File saved; verification remains.',
                'evidence': ['write-proof saved proof.txt'], 'remaining_work': ['verify file'],
                'errors': ['model request timed out']})}}]}).encode())
        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://model/v1', 'HARNESS_MODEL': 'example',
                                     'HARNESS_TURN_TIMEOUT_SECONDS': '60'}, clear=True), \
             patch.dict(RUNTIME['model'], {'request_retries': 0}), \
             patch('popot_agents.worker.http_harness.time.monotonic', side_effect=lambda: now[0]), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=respond), \
             patch('popot_agents.worker.http_harness.execute_tool', return_value='saved') as tool, \
             redirect_stderr(io.StringIO()):
            answer = run_http('Write then verify', {'tools': ['write_file']}, state=state)
        self.assertIn('File saved', answer)
        self.assertEqual(state['outcome']['status'], 'partial')
        self.assertEqual(state['outcome']['evidence'], ['write-proof saved proof.txt'])
        self.assertTrue(any(item.get('tool_call_id') == 'write-proof' for item in state['messages']))
        self.assertEqual(len(bodies), 3)
        tool.assert_called_once()

    def test_per_message_budget_reserves_final_answer_inside_parent_deadline(self):
        now = [0.0]
        requests, timeouts = [], []

        def respond(call, timeout):
            requests.append(json.loads(call.data))
            timeouts.append(timeout)
            if len(requests) == 1:
                now[0] = 2.1
                return io.BytesIO(json.dumps({'choices': [{'message': {'tool_calls': [
                    {'id': 'read', 'function': {'name': 'utc_time', 'arguments': '{}'}}]}}]}).encode())
            return io.BytesIO(b'{"choices":[{"message":{"content":"Verified partial result"}}]}')

        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://model/v1', 'HARNESS_MODEL': 'example',
                                     'HARNESS_TURN_TIMEOUT_SECONDS': '900'}, clear=True), \
             patch('popot_agents.worker.http_harness.time.monotonic', side_effect=lambda: now[0]), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=respond), \
             patch('popot_agents.worker.http_harness.execute_tool') as tool, \
             redirect_stderr(io.StringIO()):
            answer = run_http('review', {'tools': ['utc_time']}, timeout_seconds=3)
        self.assertIn('Verified partial result', answer)
        self.assertEqual(timeouts[0], 2)
        self.assertAlmostEqual(timeouts[-1], 0.9)
        self.assertNotIn('tools', requests[-1])
        tool.assert_not_called()

    def test_per_message_budget_cannot_extend_configured_timeout(self):
        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://model/v1', 'HARNESS_MODEL': 'example',
                                     'HARNESS_TURN_TIMEOUT_SECONDS': '3'}, clear=True), \
             patch('popot_agents.worker.http_harness.request.urlopen',
                   return_value=io.BytesIO(b'{"choices":[{"message":{"content":"ok"}}]}')) as call, \
             redirect_stderr(io.StringIO()):
            self.assertEqual(run_http('review', timeout_seconds=900), 'ok')
        self.assertLessEqual(call.call_args.kwargs['timeout'], 3)

    def test_invalid_per_message_budget_is_rejected_before_model_request(self):
        with patch('popot_agents.worker.http_harness.request.urlopen') as call:
            for timeout in (0, -1, float('nan'), float('inf'), True, '3'):
                with self.subTest(timeout=timeout), self.assertRaisesRegex(ValueError, 'timeout_seconds'):
                    run_http('review', timeout_seconds=timeout)
        call.assert_not_called()

    def setUp(self):
        # Explicit test budgets, independent of the operator's runtime.json values.
        for settings, overrides in ((RUNTIME['model'], {'request_retries': 1}),
                                    (RUNTIME['timeouts'], {'model_request_seconds': 45})):
            patcher = patch.dict(settings, overrides)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_reserves_final_answer_and_does_not_run_tools_after_work_budget(self):
        now = [0.0]
        bodies = []
        def respond(call, timeout):
            bodies.append(json.loads(call.data))
            if len(bodies) == 1:
                now[0] = 42.0
                return io.BytesIO(json.dumps({'choices': [{'message': {'tool_calls': [
                    {'id': 'read', 'function': {'name': 'utc_time', 'arguments': '{}'}}]}}]}).encode())
            return io.BytesIO(b'{"choices":[{"message":{"content":"Verified partial result"}}]}')
        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://model/v1',
             'HARNESS_MODEL': 'example', 'HARNESS_TURN_TIMEOUT_SECONDS': '60'}, clear=True), \
             patch('popot_agents.worker.http_harness.time.monotonic', side_effect=lambda: now[0]), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=respond), \
             patch('popot_agents.worker.http_harness.execute_tool', return_value='ok') as tool, \
             redirect_stderr(io.StringIO()):
            answer = run_http('task', {'tools': ['utc_time']})
        tool.assert_not_called()
        self.assertNotIn('tools', bodies[-1])
        self.assertIn('Verified partial result', answer)
        self.assertIn('partial result', answer.lower())

    def test_final_model_timeout_returns_partial_result_without_repeating_write(self):
        now = [0.0]
        memory_client = Mock()
        memory_client.recall.return_value = []
        calls = []
        def respond(call, timeout):
            calls.append(json.loads(call.data))
            if len(calls) == 1:
                return io.BytesIO(json.dumps({'choices': [{'message': {'tool_calls': [
                    {'id': 'write', 'function': {'name': 'write_file',
                     'arguments': '{"path":"a.txt","content":"done"}'}}]}}]}).encode())
            now[0] = 60.0
            raise TimeoutError()
        def write(*a, **kw):
            now[0] = 42.0
            return 'saved'
        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://model/v1',
             'HARNESS_MODEL': 'example', 'HARNESS_TURN_TIMEOUT_SECONDS': '60',
             'COGNEE_PASSWORD': 'private-password',
             'HARNESS_TOOL_ENV_NAMES_JSON': '["COGNEE_PASSWORD"]'}, clear=True), \
             patch('popot_agents.worker.http_harness.time.monotonic', side_effect=lambda: now[0]), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=respond), \
             patch('popot_agents.worker.http_harness.execute_tool', side_effect=write) as tool, \
             patch('popot_agents.worker.turn_memory.CogneeMemory', return_value=memory_client), \
             patch('popot_agents.worker.http_harness.mcp_client.discover_tools', return_value={}), \
             redirect_stderr(io.StringIO()):
            answer = run_http('[project: popot-agents] task', {'tools': ['write_file'],
                'mcpServers': {'cognee': {'command': ['python', 'memory.py'], 'tools': ['recall']}}})
        tool.assert_called_once()
        self.assertEqual(len(calls), 2)
        self.assertIn('partial result', answer.lower())

        self.assertEqual(memory_client.session_event.call_args.args[0]['status'], 'failed')
        memory_client.remember.assert_not_called()

    def test_model_timeouts_identify_stage_when_retries_disabled(self):
        for failure in (TimeoutError('private-detail'), URLError(TimeoutError('private-detail'))):
            with self.subTest(failure=type(failure).__name__), \
                 patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://model/v1',
                                         'HARNESS_MODEL': 'example', 'HARNESS_TURN_TIMEOUT_SECONDS': '300'},
                            clear=True), \
                 patch('popot_agents.worker.http_harness.request.urlopen', side_effect=failure) as call, \
                 patch.dict(RUNTIME['model'], {'request_retries': 0}), \
                 redirect_stderr(io.StringIO()) as logs:
                with self.assertRaisesRegex(RuntimeError, r'model request timed out.*round=1') as caught:
                    run_http('task')
                self.assertNotIn('private-detail', str(caught.exception))
                self.assertNotIn('private-detail', logs.getvalue())
                self.assertIn('model_timeout', logs.getvalue())
                call.assert_called_once()

    def test_retry_keeps_tool_results_without_reexecuting_tools(self):
        first = {'choices': [{'message': {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'write-1', 'function': {'name': 'write_file',
                                         'arguments': '{"path":"a.txt","content":"done"}'}}]}}]}
        final = b'{"choices":[{"message":{"content":"done"}}]}'
        for failure in (TimeoutError(), URLError(TimeoutError()),
                        HTTPError('http://model', 504, 'timeout', {}, io.BytesIO())):
            with self.subTest(failure=type(failure).__name__), \
                 patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://model/v1',
                                         'HARNESS_MODEL': 'example', 'HARNESS_TURN_TIMEOUT_SECONDS': '300'}, clear=True), \
                 patch.dict(RUNTIME['model'], {'request_retries': 1}), \
                 patch('popot_agents.worker.http_harness.request.urlopen', side_effect=[
                     io.BytesIO(json.dumps(first).encode()), failure, io.BytesIO(final)]) as call, \
                 patch('popot_agents.worker.http_harness.execute_tool', return_value='saved') as tool, \
                 redirect_stderr(io.StringIO()) as logs:
                self.assertEqual(run_http('task', {'tools': ['write_file'], 'max_tool_rounds': 2}), 'done')
                tool.assert_called_once()
                self.assertEqual(call.call_count, 3)
                self.assertEqual(call.call_args_list[1].args[0].data, call.call_args_list[2].args[0].data)
                self.assertIn('model_retry', logs.getvalue())

    def test_retries_are_bounded_and_permanent_errors_are_not_retried(self):
        for code, expected in ((401, 1), (403, 1), (400, 1), (504, 2), (503, 2)):
            with self.subTest(code=code), \
                 patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://model/v1',
                                         'HARNESS_MODEL': 'example', 'HARNESS_TURN_TIMEOUT_SECONDS': '300'}, clear=True), \
                 patch.dict(RUNTIME['model'], {'request_retries': 1}), \
                 patch('popot_agents.worker.http_harness.request.urlopen', side_effect=lambda *a, **kw:
                       (_ for _ in ()).throw(HTTPError('http://model', code, 'private', {}, io.BytesIO()))) as call, \
                 redirect_stderr(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, f'HTTP {code}'):
                    run_http('task')
                self.assertEqual(call.call_count, expected)

    def test_no_retry_after_turn_deadline(self):
        now = [0.0]
        def timeout(*args, **kwargs):
            now[0] = 300.0
            raise TimeoutError()
        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://model/v1',
                                     'HARNESS_MODEL': 'example', 'HARNESS_TURN_TIMEOUT_SECONDS': '300'}, clear=True), \
             patch.dict(RUNTIME['model'], {'request_retries': 1}), \
             patch('popot_agents.worker.http_harness.time.monotonic', side_effect=lambda: now[0]), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=timeout) as call, \
             redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError):
                run_http('task')
            call.assert_called_once()

    def test_timeout_reading_response_is_also_identified(self):
        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://model/v1', 'HARNESS_MODEL': 'example'},
                        clear=True), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=lambda *a, **kw: io.BytesIO()), \
             patch('popot_agents.worker.http_harness.json.load', side_effect=TimeoutError('timed out')):
            with self.assertRaisesRegex(RuntimeError, 'model request timed out'):
                run_http('task')

    def test_retry_timeout_is_capped_by_remaining_turn_budget(self):
        now = [0.0]
        timeouts = []
        def respond(call, timeout):
            timeouts.append(timeout)
            if len(timeouts) == 1:
                now[0] = 45.0
                raise TimeoutError()
            return io.BytesIO(b'{"choices":[{"message":{"content":"ok"}}]}')
        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://model/v1',
                                     'HARNESS_MODEL': 'example', 'HARNESS_TURN_TIMEOUT_SECONDS': '60'}, clear=True), \
             patch.dict(RUNTIME['model'], {'request_retries': 1}), \
             patch('popot_agents.worker.http_harness.time.monotonic', side_effect=lambda: now[0]), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=respond), \
             redirect_stderr(io.StringIO()):
            self.assertEqual(run_http('task'), 'ok')
        self.assertEqual(timeouts, [45, 15])

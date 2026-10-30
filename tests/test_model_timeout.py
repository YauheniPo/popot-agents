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

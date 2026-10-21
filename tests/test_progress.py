import asyncio
import io
import json
import tempfile
import threading
import unittest
import os
import socket
import subprocess
import sys
import importlib.util
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from popot_agents.progress import observe_progress, emit_progress, forward_progress, format_progress
from popot_agents.mcp_server import OrchestratorClient, stream_call
from popot_agents.orchestrator.main import create_server, AgentRunError


class ProgressTests(unittest.TestCase):
    def test_provider_reasoning_is_reported_before_tools_and_preserved_for_model(self):
        from popot_agents.worker.http_harness import run_http
        first = {
            'role': 'assistant', 'content': None,
            'reasoning': 'Provider explanation. test-reasoning-secret',
            'reasoning_details': [
                {'type': 'reasoning.summary', 'summary': 'Provider summary.'},
                {'type': 'reasoning.encrypted', 'data': 'opaque-provider-block', 'id': 'r1'}],
            'tool_calls': [{'id': 'tool1', 'type': 'function',
                            'function': {'name': 'utc_time', 'arguments': '{}'}}],
        }
        responses = [first, {'role': 'assistant', 'content': 'done',
                             'reasoning_content': 'Provider final explanation.'}]
        calls, received = [], []

        def respond(call, timeout):
            calls.append(json.loads(call.data))
            return io.BytesIO(json.dumps({'choices': [{'message': responses[len(calls) - 1]}]}).encode())

        def execute(*args, **kwargs):
            events = [json.loads(message) for message in received]
            self.assertIn({'event': 'assistant_reasoning', 'round': 1,
                           'reasoning': 'Provider explanation. [REDACTED]',
                           'reasoning_details': first['reasoning_details']}, events)
            return 'tool-result'

        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://model.test/v1',
                                    'HARNESS_MODEL': 'test',
                                    'TEST_API_KEY': 'test-reasoning-secret'}, clear=True), \
             patch('urllib.request.urlopen', side_effect=respond), \
             patch('popot_agents.worker.http_harness.execute_tool', side_effect=execute), \
             observe_progress(received.append), redirect_stderr(io.StringIO()):
            self.assertEqual(run_http('hello', {'tools': ['utc_time']}), 'done')
        self.assertEqual(calls[1]['messages'][1], first)
        events = [json.loads(message) for message in received]
        self.assertIn({'event': 'assistant_reasoning', 'round': 2,
                       'reasoning_content': 'Provider final explanation.'}, events)
        self.assertNotIn('test-reasoning-secret', '\n'.join(received))

    def test_no_reasoning_event_is_invented_when_provider_omits_it(self):
        from popot_agents.worker.http_harness import run_http
        received = []
        response = {'choices': [{'message': {'content': 'done', 'reasoning': None,
                                             'reasoning_details': []}}]}
        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://model.test/v1',
                                    'HARNESS_MODEL': 'test'}, clear=True), \
             patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(response).encode())), \
             observe_progress(received.append), redirect_stderr(io.StringIO()):
            self.assertEqual(run_http('hello'), 'done')
        self.assertNotIn('assistant_reasoning', [json.loads(item)['event'] for item in received])

    def test_harness_events_forward_original_fields_without_a_translation_catalog(self):
        from popot_agents.worker.http_harness import _trace
        received = []
        with observe_progress(received.append), redirect_stderr(io.StringIO()), \
             patch.dict(os.environ, {'TEST_API_KEY': 'test-secret-value'}):
            _trace('new_harness_event', phase='inspect', percent=12, details={'file': 'app.py'})
            _trace('assistant_message', content='Проверяю обработку ошибок в PR.')
            _trace('tool_error', name='mcp__github__pull_request_read', status=404,
                   error='not found', token='test-secret-value')
            _trace('assistant_answer', content='Готово. test-secret-value')
        events = [json.loads(message) for message in received]
        self.assertEqual(events, [
            {'event': 'new_harness_event', 'phase': 'inspect', 'percent': 12, 'details': {'file': 'app.py'}},
            {'event': 'assistant_message', 'content': 'Проверяю обработку ошибок в PR.'},
            {'event': 'tool_error', 'name': 'mcp__github__pull_request_read', 'status': 404,
             'error': 'not found', 'token': '[REDACTED]'},
            {'event': 'assistant_answer', 'content': 'Готово. [REDACTED]'}])

    def test_forwarded_progress_keeps_native_fields_and_rejects_invalid_frames(self):
        messages = []
        event = format_progress('new_event', agent_role='code_reviewer', content='Проверяю PR')
        with observe_progress(messages.append):
            for message in (event, 'not json', '{}', '[]', '{"event": null}', None, {}):
                forward_progress(message)
        self.assertEqual(messages, [event])

    def test_simultaneous_requests_have_separate_observers(self):
        barrier = threading.Barrier(2)
        received = [[], []]
        def run(index, event):
            with observe_progress(received[index].append):
                barrier.wait(timeout=2)
                emit_progress('tool_call', name=event)
        threads = [threading.Thread(target=run, args=(0, 'bash')),
                   threading.Thread(target=run, args=(1, 'read_file'))]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=3)
        self.assertEqual([[json.loads(item) for item in group] for group in received],
                         [[{'event': 'tool_call', 'name': 'bash'}],
                          [{'event': 'tool_call', 'name': 'read_file'}]])

    def test_original_harness_fields_and_observers_are_request_scoped(self):
        first, second = [], []
        with observe_progress(first.append):
            emit_progress('tool_call', name='bash', arguments={'command': 'git status'})
            with observe_progress(second.append):
                emit_progress('assistant_message', content='Проверяю изменения')
            emit_progress('assistant_answer', content='Готово')
        emit_progress('model_request')
        self.assertEqual([json.loads(item) for item in first], [
            {'event': 'tool_call', 'name': 'bash', 'arguments': {'command': 'git status'}},
            {'event': 'assistant_answer', 'content': 'Готово'}])
        self.assertEqual(json.loads(second[0]), {'event': 'assistant_message', 'content': 'Проверяю изменения'})


class StreamingProgressTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'requires the installed MCP SDK')
    async def test_real_mcp_transport_sends_progress_notifications_before_result(self):
        from mcp import Client
        import httpx2 as httpx
        finish = threading.Event()
        received = []
        class Chat:
            container_name = 'worker-test'
            def send(self, message):
                if message == 'fail':
                    raise AgentRunError('review failed sessionId=0123456789abcdef test-error-secret')
                forward_progress(format_progress('assistant_message', agent_role='code_reviewer', content='Проверяю PR'))
                forward_progress(format_progress('assistant_reasoning', agent_role='code_reviewer',
                                                 reasoning='Provider reasoning fixture.'))
                if not finish.wait(5): raise AgentRunError('progress was buffered until completion')
                return {'answer':'done'}
            def close(self): pass
            def is_alive(self): return True
        class Runner:
            def start_chat(self, *_): return Chat()
        async def progress(value, total=None, message=None):
            received.append((value, total, message))
            if json.loads(message).get('reasoning') == 'Provider reasoning fixture.': finish.set()
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            server = create_server({'test':Runner()}, session_dir=directory, host='127.0.0.1', port=0)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0))
                port = reservation.getsockname()[1]
            with open(os.path.join(directory, 'mcp.log'), 'wb') as log:
                process = subprocess.Popen([sys.executable, '-u', '-m', 'popot_agents.mcp_server'],
                    env={**os.environ, 'MCP_HOST':'127.0.0.1','MCP_PORT':str(port),
                         'MCP_TEST_API_KEY': 'test-error-secret',
                         'MCP_ORCHESTRATOR_URL':f'http://127.0.0.1:{server.server_port}'},
                    stdout=subprocess.DEVNULL, stderr=log)
                try:
                    url = f'http://127.0.0.1:{port}/mcp'
                    async with httpx.AsyncClient(trust_env=False, timeout=.3) as http:
                        for _ in range(60):
                            try:
                                # GET /mcp can hold an SSE stream open in SDK 2.x.
                                await http.get(f'http://127.0.0.1:{port}/')
                                break
                            except httpx.HTTPError: await asyncio.sleep(.1)
                        else:
                            log.flush()
                            self.fail('MCP server did not start: ' + Path(log.name).read_text())
                    async with Client(url) as session:
                        self.assertEqual(session.session.protocol_version, '2026-07-28')
                        tools = await session.list_tools()
                        tool = next(t for t in tools.tools if t.name == 'run_task')
                        self.assertNotIn('ctx', tool.input_schema['properties'])
                        result = await session.call_tool('run_task', {'agent':'test','task':'work'},
                            read_timeout_seconds=10, progress_callback=progress)
                        self.assertFalse(result.is_error, result)
                        data = result.structured_content or json.loads(next(c.text for c in result.content if c.type == 'text'))
                        self.assertEqual(data['answer'], 'done')
                        self.assertIn({'event': 'assistant_message', 'agent_role': 'code_reviewer', 'content': 'Проверяю PR'},
                                      [json.loads(item) for item in data['activity']])
                        self.assertIn({'event': 'assistant_reasoning', 'agent_role': 'code_reviewer',
                                       'reasoning': 'Provider reasoning fixture.'},
                                      [json.loads(item) for item in data['activity']])
                    self.assertTrue(finish.is_set())
                    self.assertEqual([r[0] for r in received], list(range(1,len(received)+1)))
                    self.assertTrue(all(r[1] is None for r in received))
                    # Raw HTTP preserves the exact token; ClientSession normally
                    # replaces it with its generated request ID.
                    async with httpx.AsyncClient(trust_env=False, timeout=10) as http:
                        for version in ('2025-11-25', '2026-07-28'):
                            for tool_name, argument in [('run_task', 'task'), ('send_message', 'message')]:
                                for meta in (None, {}, {'progressToken': ''}, {'progressToken': None},
                                             {'progressToken': 0}, {'progressToken': 'review-70'}):
                                    with self.subTest(version=version, tool=tool_name, meta=meta):
                                        params = {'name': tool_name, 'arguments': {'agent': 'test', argument: 'work'}}
                                        if meta is not None:
                                            params['_meta'] = dict(meta)
                                        if version == '2026-07-28':
                                            params.setdefault('_meta', {}).update({
                                                'io.modelcontextprotocol/protocolVersion': version,
                                                'io.modelcontextprotocol/clientCapabilities': {},
                                            })
                                        response = await http.post(url, headers={
                                            'Accept': 'application/json, text/event-stream',
                                            'MCP-Protocol-Version': version,
                                            'Mcp-Method': 'tools/call',
                                            'Mcp-Name': tool_name,
                                        }, json={'jsonrpc': '2.0', 'id': 99, 'method': 'tools/call', 'params': params})
                                        self.assertEqual(response.status_code, 200, response.text)
                                        if response.headers['content-type'].startswith('application/json'):
                                            events = [response.json()]
                                        else:
                                            events = [json.loads(line[6:]) for line in response.text.splitlines()
                                                      if line.startswith('data: ')]
                                        self.assertTrue(all('error' not in event for event in events), events)
                                        updates = [event for event in events
                                                   if event.get('method') == 'notifications/progress']
                                        token = meta.get('progressToken') if meta is not None else None
                                        if token is None or token == '':
                                            self.assertEqual(updates, [])
                                        else:
                                            self.assertTrue(updates)
                                            self.assertTrue(all(event['params']['progressToken'] == token
                                                                for event in updates))
                                            self.assertEqual(events[-1]['id'], 99)
                                        replies = [event['result'] for event in events if event.get('id') == 99]
                                        self.assertEqual(len(replies), 1)
                                        self.assertFalse(replies[0].get('isError'), replies[0])
                                        data = replies[0].get('structuredContent') or json.loads(
                                            next(item['text'] for item in replies[0]['content'] if item['type'] == 'text'))
                                        self.assertEqual(data['answer'], 'done')
                        for tool_name, argument in [('run_task', 'task'), ('send_message', 'message')]:
                            for version in ('2025-11-25', '2026-07-28'):
                                params = {'name': tool_name, 'arguments': {'agent': 'test', argument: 'fail'}}
                                if version == '2026-07-28':
                                    params['_meta'] = {'io.modelcontextprotocol/protocolVersion': version,
                                                      'io.modelcontextprotocol/clientCapabilities': {}}
                                response = await http.post(url, headers={
                                    'Accept': 'application/json, text/event-stream',
                                    'MCP-Protocol-Version': version,
                                    'Mcp-Method': 'tools/call', 'Mcp-Name': tool_name,
                                }, json={'jsonrpc': '2.0', 'id': 100, 'method': 'tools/call', 'params': params})
                                self.assertEqual(response.status_code, 200, response.text)
                                events = ([response.json()] if response.headers['content-type'].startswith('application/json')
                                          else [json.loads(line[6:]) for line in response.text.splitlines()
                                                if line.startswith('data: ')])
                                result = next(event['result'] for event in events if event.get('id') == 100)
                                self.assertTrue(result['isError'])
                                text = '\n'.join(item['text'] for item in result['content'] if item['type'] == 'text')
                                self.assertIn('HTTP 502: review failed sessionId=0123456789abcdef', text)
                                self.assertNotIn('test-error-secret', text)
                finally:
                    finish.set()
                    process.terminate()
                    try: process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=3)

    async def test_failed_task_remains_an_error_after_progress_started(self):
        class Chat:
            container_name = 'worker-test'
            def send(self, message):
                emit_progress('tool_call', name='bash')
                raise AgentRunError('provider failed')
            def close(self): pass
            def is_alive(self): return True
        class Runner:
            def start_chat(self, *_): return Chat()
        class Context:
            request_context = SimpleNamespace(meta=None)
            async def report_progress(self, *_, **__): pass
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            server = create_server({'test': Runner()}, session_dir=directory, host='127.0.0.1', port=0)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                with self.assertRaisesRegex(RuntimeError, 'HTTP 502: provider failed'):
                    await stream_call(OrchestratorClient(f'http://127.0.0.1:{server.server_port}'),
                                      '/tasks', {'agent':'test','task':'fail'}, Context())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    async def test_mcp_receives_action_before_answer_and_keeps_final_result(self):
        started, finish = threading.Event(), threading.Event()

        class Chat:
            container_name = 'worker-test'
            def send(self, message):
                for _ in range(250):
                    emit_progress('new_harness_event', value=1)
                emit_progress('tool_call', name='read_file')
                started.set()
                if not finish.wait(3):
                    raise AgentRunError('test did not receive progress before final answer')
                return {'answer': 'done'}
            def close(self): pass
            def is_alive(self): return True

        class Runner:
            def start_chat(self, *_): return Chat()

        class Context:
            request_context = SimpleNamespace(meta={'progress_token': 'test'})
            def __init__(self): self.events = []
            async def report_progress(self, progress, total=None, message=None):
                self.events.append((progress, total, message))
                if json.loads(message).get('name') == 'read_file':
                    self.assert_before_answer = not finish.is_set()
                    finish.set()

        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            server = create_server({'test': Runner()}, session_dir=directory, host='127.0.0.1', port=0)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                context = Context()
                result = await stream_call(OrchestratorClient(f'http://127.0.0.1:{server.server_port}'),
                                           '/tasks', {'agent': 'test', 'task': 'work'}, context)
                self.assertTrue(started.is_set())
                self.assertTrue(context.assert_before_answer)
                self.assertEqual(result['answer'], 'done')
                self.assertIn({'event': 'tool_call', 'name': 'read_file'}, [json.loads(item) for item in result['activity']])
                self.assertEqual(sum(json.loads(e[2]).get('event') == 'new_harness_event' for e in context.events), 250)
                self.assertEqual([e[0] for e in context.events], list(range(1, len(context.events) + 1)))
                self.assertTrue(all(e[1] is None for e in context.events))
            finally:
                finish.set()
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()

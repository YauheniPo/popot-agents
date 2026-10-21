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
import time
import importlib.util
from pathlib import Path
from contextlib import redirect_stderr
from unittest.mock import patch

from popot_agents.progress import observe_progress, emit_progress, forward_progress, format_progress
from popot_agents.mcp_server import OrchestratorClient, stream_call
from popot_agents.orchestrator.main import create_server, AgentRunError, DockerChat


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


class DockerProgressTests(unittest.TestCase):
    def test_docker_forwards_progress_before_the_worker_returns_its_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            received_file = Path(directory) / 'received'
            message = format_progress('tool_call', name='read_file')
            script = f'''import json,pathlib,sys,time
payload=json.load(sys.stdin)
if payload.get('progress') is not True: raise SystemExit(1)
print(json.dumps({{'progress': {message!r}}}), flush=True)
deadline=time.monotonic()+2
while not pathlib.Path({str(received_file)!r}).exists() and time.monotonic()<deadline: time.sleep(.01)
print(json.dumps({{'answer': 'done'}}), flush=True)
'''
            process = subprocess.Popen([sys.executable, '-u', '-c', script], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       start_new_session=True)
            received = []
            def notify(event):
                received.append(event)
                received_file.touch()
            try:
                with patch('popot_agents.orchestrator.main.subprocess.Popen', return_value=process), \
                     patch('popot_agents.orchestrator.main.subprocess.run', return_value=
                           subprocess.CompletedProcess([], 0, '{"answer":"done"}', '')), \
                     patch.object(DockerChat, '_capture_logs'), observe_progress(notify):
                    result = DockerChat('test-worker', 3).send('read a file')
                self.assertEqual(received, [message])
                self.assertEqual(result, {'answer': 'done'})
                self.assertEqual(process.returncode, 0)
                self.assertTrue(process.stdin.closed and process.stdout.closed)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=3)
                process.stdin.close()
                process.stdout.close()

    def test_docker_stream_timeout_kills_and_reaps_the_client(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        started = time.monotonic()
        try:
            with patch('popot_agents.orchestrator.main.subprocess.Popen', return_value=process), \
                 patch('popot_agents.orchestrator.main.subprocess.run', return_value=
                       subprocess.CompletedProcess([], 0, '{"answer":"done"}', '')), \
                 patch.object(DockerChat, '_capture_logs'), observe_progress(lambda _: None):
                with self.assertRaises(subprocess.TimeoutExpired) as caught:
                    DockerChat('test-worker', .15).send('PRIVATE' + '🔒' * 29993)
            self.assertLess(time.monotonic() - started, 2)
            self.assertNotIn('PRIVATE', str(caught.exception))
            self.assertIsNotNone(process.poll())
            self.assertTrue(process.stdin.closed and process.stdout.closed)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            process.stdin.close()
            process.stdout.close()

    def test_docker_preserves_worker_error_when_the_worker_closes_stdin_early(self):
        process = subprocess.Popen([sys.executable, '-u', '-c',
                                    'import os; os.close(0); print(\'{"error":"provider failed"}\')'],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            with patch('popot_agents.orchestrator.main.subprocess.Popen', return_value=process), \
                 patch.object(DockerChat, '_capture_logs'), observe_progress(lambda _: None):
                with self.assertRaisesRegex(AgentRunError, 'provider failed'):
                    DockerChat('test-worker', 3).send('🔒' * 30000)
            self.assertIsNotNone(process.poll())
            self.assertTrue(process.stdin.closed and process.stdout.closed)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            process.stdin.close()
            process.stdout.close()

    def test_docker_socket_client_reads_progress_and_fragmented_final_answer(self):
        from popot_agents.worker import session_worker
        from contextlib import redirect_stdout
        message = format_progress('tool_call', name='read_file')
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'chat.sock')
            progress_received = threading.Event()
            class Output(io.StringIO):
                def flush(self):
                    if self.getvalue().endswith('\n'):
                        progress_received.set()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(path)
                server.listen()
                def respond():
                    with server.accept()[0] as connection:
                        connection.recv(8192)
                        connection.sendall((json.dumps({'progress': message}) + '\n').encode())
                        if not progress_received.wait(1):
                            return
                        body = (json.dumps({'answer': 'готово'}, ensure_ascii=False) + '\n').encode()
                        try:
                            connection.sendall(body[:-3])
                            time.sleep(.02)
                            connection.sendall(body[-3:])
                        except OSError:
                            pass  # The regression closes the socket after the first frame.
                thread = threading.Thread(target=respond)
                thread.start()
                output = Output()
                with patch.object(session_worker, 'SOCKET_PATH', path), \
                     patch('sys.stdin', io.StringIO('{"message":"work","progress":true}')), \
                     redirect_stdout(output):
                    session_worker.client('message')
                thread.join(timeout=2)
        self.assertEqual([json.loads(line) for line in output.getvalue().splitlines()],
                         [{'progress': message}, {'answer': 'готово'}])


class StreamingProgressTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'requires the installed MCP SDK')
    async def test_real_mcp_transport_sends_progress_notifications_before_result(self):
        from mcp import Client
        import httpx2
        finish = threading.Event()
        received = []
        class Chat:
            container_name = 'worker-test'
            def send(self, message):
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
                         'MCP_ORCHESTRATOR_URL':f'http://127.0.0.1:{server.server_port}'},
                    stdout=subprocess.DEVNULL, stderr=log)
                try:
                    url = f'http://127.0.0.1:{port}/mcp'
                    async with httpx2.AsyncClient(trust_env=False, timeout=.3) as http:
                        for _ in range(60):
                            try:
                                async with http.stream('GET', url):
                                    pass  # SDK 2.x GET opens a persistent SSE stream.
                                break
                            except httpx2.HTTPError: await asyncio.sleep(.1)
                        else: self.fail('MCP server did not start: ' + open(log.name).read())
                    from popot_agents.worker import mcp_client
                    remote = {'url': url, 'bearer_token_env': 'GITHUB_PERSONAL_ACCESS_TOKEN',
                              'tools': ['list_chats']}
                    with patch.dict(os.environ, {
                            'HARNESS_TOOL_ENV_NAMES_JSON': '["GITHUB_PERSONAL_ACCESS_TOKEN"]',
                            'GITHUB_PERSONAL_ACCESS_TOKEN': 'http-sdk-test-token'}):
                        discovered = await mcp_client._discover_one('gateway', remote)
                        self.assertIn('mcp__gateway__list_chats', discovered)
                        output = await mcp_client._call_one(remote, 'list_chats', {})
                        self.assertEqual(json.loads(output), {'chats': []})
                    for mode in ('legacy', '2026-07-28'):
                        with self.subTest(protocol=mode):
                            finish.clear()
                            received.clear()
                            async with Client(url, mode=mode) as session:
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

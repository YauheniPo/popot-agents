"""Compact worker progress must be readable while the task owns its session."""
import json
import tempfile
import threading
import unittest
from urllib import request

from popot_agents.orchestrator.main import create_server
from popot_agents.progress import emit_progress


class LiveProgressTests(unittest.TestCase):
    def test_status_is_available_before_worker_finishes_without_exposing_output(self):
        entered, release = threading.Event(), threading.Event()
        failures = []

        class Chat:
            container_name = 'worker-test'
            def send(self, message):
                emit_progress('model_request', model='test', round=1)
                emit_progress('tool_call', name='read_file', arguments={'path': 'private-source'})
                emit_progress('tool_error', name='read_file', error_type='TimeoutError', error='private-output')
                emit_progress('assistant_reasoning', reasoning='private-reasoning')
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('test worker was not released')
                return {'answer': 'done'}
            def is_alive(self):
                if entered.is_set() and not release.is_set():
                    raise AssertionError('status must not probe a busy worker')
                return True
            def close(self): pass
        class Runner:
            def start_chat(self, *_): return Chat()

        with tempfile.TemporaryDirectory() as directory:
            server = create_server({'test': Runner()}, session_dir=directory, host='127.0.0.1', port=0)
            serving = threading.Thread(target=server.serve_forever)
            serving.start()
            url = f'http://127.0.0.1:{server.server_port}'
            def read(path):
                with request.urlopen(url + path, timeout=.5) as response:
                    return json.load(response)
            def run():
                try:
                    call = request.Request(url + '/tasks', data=b'{"agent":"test","task":"work"}',
                                           headers={'Content-Type': 'application/json'})
                    with request.urlopen(call, timeout=8) as response:
                        json.load(response)
                except Exception as exc: failures.append(exc)
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                chats = read('/chats')['chats']
                sid = chats[0]['sessionId']
                detail = read('/chats/' + sid)
                for item in (chats[0], detail):
                    progress = item['progress']
                    self.assertEqual(progress['state'], 'running')
                    self.assertEqual(progress['modelCalls'], 1)
                    self.assertEqual(progress['toolCalls'], 1)
                    self.assertEqual(progress['lastAction']['event'], 'tool_error')
                    self.assertEqual(progress['lastError']['error_type'], 'TimeoutError')
                    self.assertNotIn('private-', json.dumps(progress))
                self.assertEqual(detail['status'], 'running')
                release.set()
                worker.join(3)
                progress = read('/chats/' + sid)['progress']
                self.assertEqual(progress['state'], 'completed')
                self.assertEqual(progress['lastAction']['event'], 'completed')
                followup = request.Request(url + '/messages', data=json.dumps(
                    {'sessionId': sid, 'message': 'again'}).encode(),
                    headers={'Content-Type': 'application/json'})
                with request.urlopen(followup, timeout=1) as response:
                    json.load(response)
                progress = read('/chats/' + sid)['progress']
                self.assertEqual(progress['modelCalls'], 1)
                self.assertEqual(progress['toolCalls'], 1)
            finally:
                release.set()
                worker.join(3)
                server.shutdown()
                serving.join(3)
                server.server_close()
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])

    def test_progress_is_bounded_and_excludes_reasoning_and_tool_payloads(self):
        from popot_agents.progress import TurnProgress
        progress = TurnProgress()
        for _ in range(1000):
            progress.update(json.dumps({'event': 'tool_call', 'name': 'x' * 10000,
                                        'arguments': 'private-args', 'output': 'private-output'}))
        progress.update(json.dumps({'event': 'assistant_reasoning', 'reasoning': 'private-reasoning'}))
        snapshot = progress.snapshot()
        self.assertEqual(snapshot['toolCalls'], 1000)
        self.assertEqual(snapshot['lastAction']['event'], 'tool_call')
        self.assertLess(len(json.dumps(snapshot)), 1500)
        self.assertNotIn('private-', json.dumps(snapshot))
        progress.update('{"event":"mcp_discovery_started","server":"github","attempt":2}')
        self.assertEqual(progress.snapshot()['lastAction'],
                         {'event': 'mcp_discovery_started', 'server': 'github', 'attempt': 2})
        progress.update('{"event":"completed","agent_role":"child"}')
        self.assertEqual(progress.snapshot()['state'], 'running')
        progress.update('{"event":"failed","error":"private-failure"}', finished=True)
        snapshot = progress.snapshot()
        self.assertEqual(snapshot['state'], 'failed')
        snapshot['lastError']['event'] = 'mutated'
        self.assertEqual(progress.snapshot()['lastError']['event'], 'failed')

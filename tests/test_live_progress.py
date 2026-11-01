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
                child_id = 'b' * 16
                commit_url = 'https://github.com/example/team/commit/' + 'c' * 40
                server.store.save(child_id, 'test', [{'role': 'user', 'content': 'private-child-task'}],
                    'backend_engineer', {'_ax_delegation': {'root_session_id': sid,
                        'parent_session_id': sid, 'state': 'TASK_STATE_WORKING',
                        'input': 'private-child-context', 'outcome': {'status': 'partial',
                        'evidence': ['Published ' + commit_url, 'CI workspace tests: success'],
                        'remaining_work': [], 'errors': []}}})
                detail = read('/chats/' + sid)
                self.assertEqual(detail['work']['rootSessionId'], sid)
                receipt = next(item for item in detail['work']['tasks'] if item['sessionId'] == child_id)
                self.assertEqual(receipt['outcome']['status'], 'partial')
                self.assertIn('CI workspace tests: success', receipt['outcome']['evidence'])
                self.assertIn(commit_url, [item['url'] for item in receipt['references']])
                self.assertNotIn('private-', json.dumps(detail['work']))
                self.assertEqual(read('/chats/' + child_id)['status'], 'running')
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

    def test_error_has_time_source_and_counts_later_activity_without_claiming_recovery(self):
        from popot_agents.progress import TurnProgress, scoped_progress
        progress = TurnProgress('a' * 16)
        message = scoped_progress('backend_engineer', json.dumps({
            'event': 'tool_error', 'name': 'delegate_task', 'error_type': 'ToolInputError',
            'recoverable': True, 'error': 'Child not started: delegation needs at least 60 seconds of work budget.'}),
            session_id='b' * 16)
        message = scoped_progress('planner', message, session_id='a' * 16)
        progress.update(message)
        first = progress.snapshot()
        self.assertEqual(first['lastError']['agent_session_id'], 'b' * 16)
        self.assertTrue(first['lastError']['observedAt'])
        self.assertIn('60 seconds', first['lastError']['message'])
        self.assertEqual(first['eventsAfterLastError'], 0)
        progress.update(json.dumps({'event': 'model_request'}))
        second = progress.snapshot()
        self.assertEqual(second['eventsAfterLastError'], 1)
        self.assertEqual(second['lastError'], first['lastError'])
        self.assertEqual(second['lastAction']['agent_session_id'], 'a' * 16)

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
        action = progress.snapshot()['lastAction']
        self.assertEqual({key: action[key] for key in ('event', 'server', 'attempt')},
                         {'event': 'mcp_discovery_started', 'server': 'github', 'attempt': 2})
        self.assertTrue(action['observedAt'])
        progress.update('{"event":"completed","agent_role":"child"}')
        self.assertEqual(progress.snapshot()['state'], 'running')
        progress.update('{"event":"failed","error":"private-failure"}', finished=True)
        snapshot = progress.snapshot()
        self.assertEqual(snapshot['state'], 'failed')
        snapshot['lastError']['event'] = 'mutated'
        self.assertEqual(progress.snapshot()['lastError']['event'], 'failed')

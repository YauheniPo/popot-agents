"""Concurrent retries must share one execution, including worker startup."""
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import patch
from urllib import error, request

from popot_agents.orchestrator.main import AgentRunError, create_server


class Chat:
    container_name = 'test-coordinator'

    def __init__(self, sends):
        self.sends = sends

    def send(self, message, **_kwargs):
        self.sends.append(message)
        return {'answer': 'verified', 'outcome': {'status': 'completed',
            'reason': 'verified', 'evidence': ['receipt'], 'remaining_work': [], 'errors': []}}

    def restore(self, messages, **_kwargs):
        pass

    def is_alive(self):
        return True

    def close(self):
        pass


class WorkflowConcurrencyTests(unittest.TestCase):
    @contextmanager
    def running_server(self, *, max_concurrent=4):
        entered, release = threading.Event(), threading.Event()
        starts, sends = [], []

        class Runner:
            block_next = True

            def start_chat(self, sid, config, **_kwargs):
                starts.append(sid)
                if self.block_next:
                    self.block_next = False
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError('test did not release worker startup')
                return Chat(sends)

        runner = Runner()
        with tempfile.TemporaryDirectory() as directory:
            server = create_server({'test': runner}, roles={'planner': {'agent': 'test'}},
                session_dir=directory, host='127.0.0.1', port=0,
                workflow_policy={'max_turns': 4, 'total_seconds': 60, 'turn_seconds': 30,
                                 'max_no_progress': 2, 'max_concurrent': max_concurrent})
            server.service_actions = lambda: None
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                yield server, runner, entered, release, starts, sends
            finally:
                release.set()
                server.shutdown()
                thread.join()
                server.server_close()

    def post(self, server, payload, path='/tasks'):
        call = request.Request(f'http://127.0.0.1:{server.server_port}{path}',
            data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
        try:
            response = request.urlopen(call, timeout=4)
        except error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response)

    def test_same_request_id_is_claimed_before_worker_startup(self):
        payload = {'role': 'planner', 'task': 'publish once', 'requestId': 'same-operation'}
        with self.running_server() as (server, _, entered, release, starts, sends):
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(self.post, server, payload)
                try:
                    self.assertTrue(entered.wait(2))
                    status, duplicate = self.post(server, payload)
                    self.assertEqual(status, 409)
                    self.assertEqual(duplicate['sessionId'], starts[0])
                    self.assertEqual(len(starts), 1)
                finally:
                    release.set()
                status, completed = first.result(timeout=3)
                self.assertEqual(status, 200)
            self.assertEqual(sends, ['publish once'])
            self.assertEqual(self.post(server, payload)[1]['sessionId'], completed['sessionId'])
            self.assertEqual(len(starts), 1)

    def test_conflicting_request_is_rejected_during_startup(self):
        payload = {'role': 'planner', 'task': 'publish once', 'requestId': 'same-operation'}
        with self.running_server() as (server, _, entered, release, starts, _):
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(self.post, server, payload)
                try:
                    self.assertTrue(entered.wait(2))
                    for change in ({'task': 'different task'}, {'disableDelegation': True}):
                        with self.subTest(change=change):
                            self.assertEqual(self.post(server, {**payload, **change})[0], 409)
                    self.assertEqual(len(starts), 1)
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=3)[0], 200)

    def test_unrelated_request_can_finish_while_first_worker_starts(self):
        payload = {'role': 'planner', 'task': 'first', 'requestId': 'first'}
        with self.running_server() as (server, _, entered, release, starts, _):
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(self.post, server, payload)
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertEqual(self.post(server, {**payload, 'requestId': 'second'})[0], 200)
                    self.assertEqual(len(starts), 2)
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=3)[0], 200)

    def test_startup_failure_releases_request_claim(self):
        payload = {'role': 'planner', 'task': 'publish once', 'requestId': 'retry-startup'}
        with self.running_server() as (server, runner, _, release, _, sends):
            release.set()
            with patch.object(runner, 'start_chat', side_effect=AgentRunError('startup unavailable')):
                self.assertEqual(self.post(server, payload)[0], 502)
            self.assertEqual(self.post(server, payload)[0], 200)
            self.assertEqual(sends, ['publish once'])

    def test_capacity_rejection_releases_request_claim(self):
        first_payload = {'role': 'planner', 'task': 'first', 'requestId': 'first'}
        second_payload = {**first_payload, 'task': 'second', 'requestId': 'second'}
        with self.running_server(max_concurrent=1) as (server, _, entered, release, _, sends):
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(self.post, server, first_payload)
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertEqual(self.post(server, second_payload)[0], 429)
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=3)[0], 200)
            self.assertEqual(self.post(server, second_payload)[0], 200)
            self.assertEqual(sends, ['first', 'second'])

    def test_existing_session_rechecks_retry_after_waiting_for_its_lock(self):
        with self.running_server() as (server, runner, entered, release, starts, sends):
            release.set()
            _, saved = self.post(server, {'role': 'planner', 'task': 'initial'})
            sid = saved['sessionId']
            server.live.pop(sid).worker.close()
            entered.clear()
            release.clear()
            runner.block_next = True
            second_waiting = threading.Event()
            original_lock = server.session_lock
            lock_calls = []

            def session_lock(session_id):
                lock_calls.append(session_id)
                if len(lock_calls) == 2:
                    second_waiting.set()
                return original_lock(session_id)

            server.session_lock = session_lock
            payload = {'sessionId': sid, 'message': 'follow-up once', 'requestId': 'follow-up'}
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.post, server, payload, '/messages')
                try:
                    self.assertTrue(entered.wait(2))
                    second = pool.submit(self.post, server, payload, '/messages')
                    self.assertTrue(second_waiting.wait(2))
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=3)[0], 200)
                self.assertEqual(second.result(timeout=3)[0], 200)
            self.assertEqual(sends, ['initial', 'follow-up once'])
            self.assertEqual(len(starts), 2)

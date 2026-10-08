import io
import json
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ax_local.api.ax_runner import AxAgentRunner, AxChat
from ax_local.config import AX_CONFIG
from popot_agents.orchestrator.main import AgentRunError, ChatServer, create_server
from popot_agents.orchestrator.session_store import SessionStore
from tests.test_chat import FakeChat
from tests import test_chat as chat_tests


class ParkedChat(FakeChat):
    idle_timeout_seconds = 7200

    def __init__(self, name, store, session_id):
        super().__init__(name)
        self.store = store
        self.session_id = session_id
        self.suspended = False
        self.suspend_count = 0
        self.resume_count = 0

    @property
    def status(self):
        return 'suspended' if self.suspended else 'running'

    def after_turn(self):
        self.assert_saved = self.store.get(self.session_id)['messages'][-1]['content']
        self.suspended = True
        self.suspend_count += 1

    def send(self, message):
        self.resume()
        return super().send(message)

    def resume(self):
        if self.suspended:
            self.resume_count += 1
            self.suspended = False


class SessionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SessionStore(self.directory.name)
        self.chats = []
        runner = SimpleNamespace(start_chat=self.start_chat, retain_session_history=True)
        with patch('popot_agents.orchestrator.main.ChatServer') as server_class:
            create_server({'test_agent': runner})
        self.handler = server_class.call_args.args[1]
        self.locks = {}
        self.server = SimpleNamespace(
            store=self.store, live={}, live_lock=threading.RLock(),
            session_lock=lambda sid: self.locks.setdefault(sid, threading.RLock()),
            task_slots=threading.BoundedSemaphore(3), pending_expired=set(),
            idle_seconds=900, last_prune=time.monotonic(),
        )

    def start_chat(self, session_id, role_config=None):
        chat = ParkedChat('worker-' + session_id, self.store, session_id)
        self.chats.append(chat)
        return chat

    def post(self, payload):
        return chat_tests.TaskSessionTests._post_to_handler(self.handler, self.server, payload)

    def test_answer_is_saved_before_suspend_and_followup_resumes_same_worker(self):
        status, first = self.post({'agent': 'test_agent', 'message': 'first task'})
        self.assertEqual(status, 200)
        chat = self.chats[0]
        self.assertTrue(chat.suspended)
        self.assertEqual(chat.assert_saved, first['answer'])
        status, second = self.post({'sessionId': first['sessionId'], 'message': 'continue'})
        self.assertEqual(status, 200)
        self.assertEqual(len(self.chats), 1)
        self.assertEqual(chat.resume_count, 1)
        self.assertEqual(chat.suspend_count, 2)
        self.assertEqual(second['answer'], 'turn 2')

    def test_worker_survives_previous_idle_timeout_and_expires_at_two_hours(self):
        _, first = self.post({'agent': 'test_agent', 'message': 'first task'})
        sid = first['sessionId']
        record = self.server.live[sid]
        with patch('popot_agents.orchestrator.main.time.monotonic', return_value=10000):
            record.last_used = 10000 - 901
            ChatServer.service_actions(self.server)
            self.assertFalse(self.chats[0].closed)
            record.last_used = 10000 - 7200
            ChatServer.service_actions(self.server)
        self.assertTrue(self.chats[0].closed)
        self.assertNotIn(sid, self.server.live)
        self.assertIsNotNone(self.store.get(sid))
        status, second = self.post({'sessionId': sid, 'message': 'continue after expiry'})
        self.assertEqual(status, 200)
        self.assertEqual(len(self.chats), 2)
        self.assertEqual(self.chats[1].restored, [
            {'role': 'user', 'content': 'first task'},
            {'role': 'assistant', 'content': 'turn 1'},
        ])
        self.assertEqual(second['answer'], 'turn 2')
        self.assertTrue(self.chats[1].suspended)

    def test_followup_enforces_expiry_even_before_janitor_runs(self):
        _, first = self.post({'agent': 'test_agent', 'message': 'first task'})
        sid = first['sessionId']
        self.server.live[sid].last_used = time.monotonic() - 7201
        status, _ = self.post({'sessionId': sid, 'message': 'continue'})
        self.assertEqual(status, 200)
        self.assertTrue(self.chats[0].closed)
        self.assertEqual(self.chats[0].resume_count, 0)
        self.assertEqual(len(self.chats), 2)
        self.assertTrue(self.chats[1].restored)

    def test_failed_resume_restores_history_and_sends_next_turn_once(self):
        _, first = self.post({'agent': 'test_agent', 'message': 'first task'})
        sid = first['sessionId']
        original = self.chats[0]
        with patch.object(original, 'resume', side_effect=AgentRunError('snapshot failed')), \
             patch.object(original, 'send', wraps=original.send) as old_send:
            status, second = self.post({'sessionId': sid, 'message': 'continue'})
        self.assertEqual(status, 200, second)
        self.assertTrue(original.closed)
        old_send.assert_not_called()
        self.assertEqual(len(self.chats), 2)
        self.assertEqual(self.chats[1].restored, [
            {'role': 'user', 'content': 'first task'},
            {'role': 'assistant', 'content': 'turn 1'},
        ])
        self.assertEqual(self.store.get(sid)['messages'], [
            {'role': 'user', 'content': 'first task'},
            {'role': 'assistant', 'content': 'turn 1'},
            {'role': 'user', 'content': 'continue'},
            {'role': 'assistant', 'content': 'turn 2'},
        ])

    def test_ax_history_remains_available_after_default_retention(self):
        _, first = self.post({'agent': 'test_agent', 'message': 'first task'})
        sid = first['sessionId']
        path = Path(self.directory.name) / (sid + '.json')
        saved = json.loads(path.read_text())
        saved['createdAt'] = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        path.write_text(json.dumps(saved))
        self.assertIsNotNone(self.store.get(sid))
        self.assertIsNone(self.store.expires_at(saved))
        self.assertNotIn(sid, self.store.prune_expired())

    def test_followup_marks_existing_session_history_persistent_immediately(self):
        _, first = self.post({'agent': 'test_agent', 'message': 'first task'})
        sid = first['sessionId']
        path = Path(self.directory.name) / (sid + '.json')
        saved = json.loads(path.read_text())
        saved['roleConfig'].pop('_retain_history')
        path.write_text(json.dumps(saved))
        self.server.live[sid].role_config = saved['roleConfig']
        status, _ = self.post({'sessionId': sid, 'message': 'continue'})
        self.assertEqual(status, 200)
        self.assertIsNone(self.store.expires_at(self.store.get(sid)))

    def test_expiry_does_not_close_session_owned_by_an_active_request(self):
        _, first = self.post({'agent': 'test_agent', 'message': 'first task'})
        sid = first['sessionId']
        self.server.live[sid].last_used = time.monotonic() - 7201
        held, release = threading.Event(), threading.Event()
        def own_session():
            with self.server.session_lock(sid):
                held.set()
                release.wait(3)
        thread = threading.Thread(target=own_session)
        thread.start()
        try:
            self.assertTrue(held.wait(2))
            ChatServer.service_actions(self.server)
            self.assertFalse(self.chats[0].closed)
        finally:
            release.set()
            thread.join(3)

    def test_expiry_applies_even_when_role_requests_unlimited_worker_lifetime(self):
        _, first = self.post({'agent': 'test_agent', 'message': 'first task'})
        sid = first['sessionId']
        self.server.live[sid].role_config['ttl_seconds'] = 0
        self.server.live[sid].last_used = time.monotonic() - 7201
        ChatServer.service_actions(self.server)
        self.assertTrue(self.chats[0].closed)
        self.assertIsNotNone(self.store.get(sid))

    def test_listing_suspended_session_reports_status_without_resuming(self):
        _, first = self.post({'agent': 'test_agent', 'message': 'first task'})
        handler = self.handler.__new__(self.handler)
        handler.path = '/chats'
        handler.server = self.server
        responses = []
        handler._reply = lambda status, result: responses.append((status, result))
        handler._get()
        self.assertEqual(responses[0][1]['chats'][0]['status'], 'suspended')
        handler._get_chat(first['sessionId'])
        self.assertEqual(responses[1][1]['status'], 'suspended')
        self.assertEqual(self.chats[0].resume_count, 0)


class AxLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.runner = AxAgentRunner(
            image='localhost:5001/worker@sha256:' + 'a' * 64,
            model='test', base_url='http://localhost:1/v1',
        )
        self.chat = AxChat(self.runner, 'popot-chat-0123456789abcdef', 12)

    def test_suspend_waits_for_confirmed_phase_and_probe_does_not_wake_worker(self):
        with patch.object(self.runner, '_run', return_value=subprocess.CompletedProcess([], 0)), \
             patch.object(self.runner, 'task_status', side_effect=[('Running', None), ('Suspended', None)]), \
             patch('ax_local.api.ax_runner.time.sleep'), \
             patch.object(self.runner, 'remote') as remote:
            self.chat.after_turn()
            self.assertEqual(self.chat.status, 'suspended')
            self.assertTrue(self.chat.is_alive())
        remote.assert_not_called()

    def test_next_message_resumes_then_waits_for_worker_before_sending(self):
        self.chat.suspended = True
        with patch.object(self.runner, '_run', return_value=subprocess.CompletedProcess([], 0)) as run, \
             patch.object(self.runner, 'remote', side_effect=[{'status': 'ok'}, {'answer': 'continued'}]) as remote:
            self.assertEqual(self.chat.send('continue'), {'answer': 'continued'})
        self.assertEqual(run.call_args_list[0].args[:3],
                         ('resume', 'task', self.chat.container_name))
        self.assertEqual(remote.call_args_list[0].args[1], {'action': 'ping'})
        self.assertEqual(remote.call_args_list[1].args[1]['message'], 'continue')

    def test_suspend_failure_keeps_successful_answer_and_closes_worker(self):
        with patch.object(self.runner, '_run', side_effect=[
                subprocess.TimeoutExpired(['ax', 'private-value'], 1),
                subprocess.CompletedProcess([], 0)]) as run, \
             patch('sys.stderr', new_callable=io.StringIO) as log:
            self.chat.after_turn()
        self.assertTrue(self.chat.closed)
        self.assertIn('AX suspension not confirmed', log.getvalue())
        self.assertNotIn('private-value', log.getvalue())
        self.assertEqual(run.call_args_list[-1].args[0], 'delete')

    def test_failed_resume_never_sends_model_turn_and_allows_history_restore(self):
        self.chat.suspended = True
        with patch.object(self.runner, '_run', side_effect=[
                subprocess.TimeoutExpired(['ax', 'private-value'], 1),
                subprocess.CompletedProcess([], 0)]), \
             patch.object(self.runner, 'remote') as remote:
            with self.assertRaisesRegex(Exception, 'restore saved history') as caught:
                self.chat.send('continue')
        self.assertNotIn('private-value', str(caught.exception))
        self.assertTrue(self.chat.closed)
        remote.assert_not_called()

    def test_resumed_task_with_missing_chat_socket_fails_without_full_startup_wait(self):
        self.chat.suspended = True
        with patch.object(self.runner, '_run', return_value=subprocess.CompletedProcess([], 0)), \
             patch.object(self.runner, 'task_status', return_value=('Running', None)), \
             patch.object(self.runner, 'remote', side_effect=AgentRunError(
                 'FileNotFoundError: [Errno 2] No such file or directory')) as remote, \
             patch('ax_local.api.ax_runner.time.sleep', side_effect=AssertionError('must fail promptly')):
            with self.assertRaises(AgentRunError):
                self.chat.resume()
        self.assertTrue(self.chat.closed)
        self.assertEqual(remote.call_count, 1)


if __name__ == '__main__':
    unittest.main()

import io
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from ax_local.api.ax_runner import AxAgentRunner
from ax_local.api.worker_queue import WorkerQueue
from ax_local.config import AX_CONFIG
from popot_agents.orchestrator.main import AgentRunError, ChatServer, create_server
from tests.test_chat import FakeRunner
from tests import test_chat as chat_tests


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('worker queue did not reach the expected state')
        time.sleep(0.001)


class WorkerQueueTests(unittest.TestCase):
    def test_fifo_admission_after_ten_slots_are_occupied(self):
        queue = WorkerQueue(10, 3600)
        leases = [queue.acquire(str(i)) for i in range(10)]
        order, errors = [], []
        def start(name):
            try:
                lease = queue.acquire(name)
                order.append(name)
                queue.release(name, lease)
            except Exception as exc:
                errors.append(exc)
        threads = []
        for index, name in enumerate(('eleventh', 'twelfth'), 1):
            thread = threading.Thread(target=start, args=(name,))
            thread.start()
            threads.append(thread)
            wait_for(lambda: queue.status()['queued'] == index)
        self.assertEqual(order, [])
        queue.release('0', leases[0])
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(order, ['eleventh', 'twelfth'])
        self.assertEqual(queue.status()['active'], 9)

    def test_one_hour_boundary_expires_before_a_new_slot_is_granted(self):
        now = [0.0]
        queue = WorkerQueue(1, 3600, clock=lambda: now[0])
        occupied = queue.acquire('first')
        errors = []
        def start():
            try:
                queue.acquire('waiting')
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=start)
        thread.start()
        wait_for(lambda: queue.status()['queued'] == 1)
        now[0] = 3600
        queue.release('first', occupied)
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], AgentRunError)
        self.assertIn('3600', str(errors[0]))
        self.assertEqual(queue.status()['active'], 0)
        self.assertEqual(queue.status()['queued'], 0)

    def test_canceled_head_does_not_block_the_next_request(self):
        queue = WorkerQueue(1, 3600)
        occupied = queue.acquire('first')
        canceled = threading.Event()
        results = []
        def start():
            try:
                queue.acquire('canceled', cancel_event=canceled)
            except TimeoutError:
                results.append('canceled')
        thread = threading.Thread(target=start)
        thread.start()
        wait_for(lambda: queue.status()['queued'] == 1)
        canceled.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, ['canceled'])
        queue.release('first', occupied)
        queue.acquire('next')
        self.assertEqual(queue.status()['queued'], 0)

    def test_shutdown_wakes_all_waiters_and_prevents_new_starts(self):
        queue = WorkerQueue(1, 3600)
        queue.acquire('first')
        errors = []
        def start():
            try:
                queue.acquire('waiting')
            except AgentRunError as exc:
                errors.append(exc)
        thread = threading.Thread(target=start)
        thread.start()
        wait_for(lambda: queue.status()['queued'] == 1)
        queue.close()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        with self.assertRaises(AgentRunError):
            queue.acquire('new')

    def test_shorter_parent_deadline_limits_wait_and_old_lease_cannot_release_replacement(self):
        now = [0.0]
        queue = WorkerQueue(1, 3600, clock=lambda: now[0])
        first = queue.acquire('first')
        errors = []
        def start():
            try:
                queue.acquire('child', deadline=60)
            except TimeoutError as exc:
                errors.append(exc)
        thread = threading.Thread(target=start)
        thread.start()
        wait_for(lambda: queue.status()['queued'] == 1)
        now[0] = 60
        queue.release('first', first)
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        replacement = queue.acquire('first')
        queue.release('first', first)
        self.assertEqual(queue.status()['active'], 1)
        queue.release('first', replacement)



class AxWorkerCapacityTests(unittest.TestCase):
    def setUp(self):
        self.runner = AxAgentRunner(image='localhost:5001/popot-agent-ax@sha256:' + 'a' * 64,
                                    model='unused', base_url='http://localhost:1/v1')

    @staticmethod
    def cli(*args, **_kwargs):
        if args[0] == 'get':
            return subprocess.CompletedProcess([], 1, '', 'NotFound')
        return subprocess.CompletedProcess([], 0, '', '')

    def test_eleventh_worker_is_not_created_until_one_of_ten_is_deleted(self):
        created, errors = [], []
        with patch.object(self.runner, '_run', side_effect=self.cli) as run, \
             patch.object(self.runner, '_wait_for_chat', side_effect=lambda chat, *_args: chat):
            chats = [self.runner.start_chat(f'{i:016x}') for i in range(10)]
            chats[0].suspended = True
            def start():
                try:
                    created.append(self.runner.start_chat(f'{10:016x}'))
                except Exception as exc:
                    errors.append(exc)
            thread = threading.Thread(target=start)
            thread.start()
            wait_for(lambda: self.runner.worker_queue.status()['queued'] == 1)
            self.assertEqual(len([call for call in run.call_args_list if call.args[0] == 'apply']), 20)
            self.assertTrue(chats[0].is_alive())
            chats[0].close()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(created), 1)
            self.assertEqual(self.runner.worker_queue.status()['active'], 10)
            for chat in chats + created:
                chat.close()
        self.assertEqual(self.runner.worker_queue.status()['active'], 0)

    def test_failed_start_and_double_close_do_not_leak_or_duplicate_slots(self):
        with patch.object(self.runner, '_run', side_effect=self.cli), \
             patch.object(self.runner, '_wait_for_chat', side_effect=AgentRunError('not ready')):
            with self.assertRaises(AgentRunError):
                self.runner.start_chat('a' * 16)
        self.assertEqual(self.runner.worker_queue.status()['active'], 0)

    def test_shutdown_interrupts_queued_http_request_before_joining_request_threads(self):
        from http.client import HTTPConnection
        with tempfile.TemporaryDirectory() as directory:
            server = create_server({'test': self.runner}, port=0, session_dir=directory)
            for index in range(10):
                self.runner.worker_queue.acquire(f'occupied-{index}')
            serving = threading.Thread(target=server.serve_forever)
            serving.start()
            replies = []
            def post():
                connection = HTTPConnection(*server.server_address, timeout=3)
                try:
                    connection.request('POST', '/messages', '{"agent":"test","message":"waiting"}',
                                       {'Content-Type': 'application/json'})
                    reply = connection.getresponse()
                    replies.append(reply.status)
                    reply.read()
                finally:
                    connection.close()
            client = threading.Thread(target=post)
            try:
                client.start()
                wait_for(lambda: self.runner.worker_queue.status()['queued'] == 1)
                server.shutdown()
                closing = threading.Thread(target=server.server_close)
                closing.start()
                closing.join(2)
                self.assertFalse(closing.is_alive(), 'server must wake queue before waiting for HTTP threads')
                client.join(2)
                self.assertFalse(client.is_alive())
                self.assertEqual(replies, [502])
            finally:
                self.runner.shutdown_worker_queue()
                server.shutdown()
                server.server_close()
                serving.join(2)
                client.join(2)

    def test_restart_cleanup_targets_only_popot_workers_and_aborts_on_delete_failure(self):
        listed = subprocess.CompletedProcess([], 0,
            'NAME ATESPACE PHASE\npopot-chat-aaaaaaaaaaaaaaaa local Suspended\n'
            'unrelated-task local Running\n', '')
        with patch.object(self.runner, '_run', side_effect=[listed, AgentRunError('cleanup failed')]) as run:
            with self.assertRaises(AgentRunError):
                self.runner.recover_workers()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args, ('delete', 'task', 'popot-chat-aaaaaaaaaaaaaaaa'))

    def test_unconfirmed_deletion_keeps_slot_until_maintenance_confirms_cleanup(self):
        with patch.object(self.runner, '_run', side_effect=self.cli), \
             patch.object(self.runner, '_wait_for_chat', side_effect=lambda chat, *_args: chat):
            chat = self.runner.start_chat('a' * 16)
        with patch.object(self.runner, '_run', side_effect=subprocess.TimeoutExpired(['ax'], 5)), \
             redirect_stderr(io.StringIO()):
            chat.close()
            chat.close()
        self.assertEqual(self.runner.worker_queue.status()['active'], 1)
        with patch.object(self.runner, '_run', side_effect=self.cli):
            self.runner.maintenance()
        self.assertEqual(self.runner.worker_queue.status()['active'], 0)

    def test_failed_previous_task_deletion_retains_capacity_for_cleanup(self):
        present = subprocess.CompletedProcess([], 0, '', '')
        with patch.object(self.runner, '_run', side_effect=[present, AgentRunError('delete failed')]):
            with self.assertRaises(AgentRunError):
                self.runner.start_chat('a' * 16)
        self.assertEqual(self.runner.worker_queue.status()['active'], 1)
        with patch.object(self.runner, '_run', side_effect=self.cli):
            self.runner.maintenance()
        self.assertEqual(self.runner.worker_queue.status()['active'], 0)

    def test_deadline_after_previous_task_lookup_retains_slot_until_cleanup(self):
        present = subprocess.CompletedProcess([], 0, '', '')
        with patch.object(self.runner.worker_queue, 'clock', return_value=0), \
             patch('ax_local.api.ax_runner.time.monotonic', side_effect=[0, 2]), \
             patch.object(self.runner, '_run', return_value=present) as run:
            with self.assertRaises(TimeoutError):
                self.runner.start_chat('a' * 16, deadline=1)
        self.assertEqual(run.call_count, 1, 'deadline must stop deletion before another CLI call')
        self.assertEqual(self.runner.worker_queue.status()['active'], 1)
        with patch.object(self.runner, '_run', side_effect=self.cli):
            self.runner.maintenance()
        self.assertEqual(self.runner.worker_queue.status()['active'], 0)

    def test_queue_timeout_returns_503_without_starting_any_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            server = create_server({'test': self.runner}, port=0, session_dir=directory)
            self.runner.worker_queue.timeout_seconds = 0.02
            for index in range(10):
                self.runner.worker_queue.acquire(f'occupied-{index}')
            try:
                with patch.object(self.runner, '_run') as run:
                    status, result = chat_tests.TaskSessionTests._post_to_handler(
                        server.RequestHandlerClass, server, {'agent': 'test', 'message': 'waiting'})
                self.assertEqual(status, 503, result)
                self.assertIn('queue timed out', result['error'])
                run.assert_not_called()
                self.assertEqual(server.store.list_chats(), [])
            finally:
                server.server_close()

    def test_http_admission_does_not_reject_ax_requests_at_previous_three_request_limit(self):
        runner = FakeRunner()
        runner.queues_worker_start = True
        with tempfile.TemporaryDirectory() as directory:
            server = create_server({'test': runner}, port=0, session_dir=directory)
            try:
                for _ in range(3):
                    self.assertTrue(server.task_slots.acquire(blocking=False))
                status, result = chat_tests.TaskSessionTests._post_to_handler(
                    server.RequestHandlerClass, server, {'agent': 'test', 'message': 'hello'})
                self.assertEqual(status, 200, result)
                self.assertEqual(len(runner.chats), 1)
            finally:
                for _ in range(3):
                    server.task_slots.release()
                server.server_close()

    def test_unrelated_sessions_never_share_a_lock_during_queue_wait(self):
        buckets = {}
        for index in range(65):
            sid = f'{index:016x}'
            bucket = hash(sid) % 64
            if bucket in buckets:
                other = buckets[bucket]
                break
            buckets[bucket] = sid
        with tempfile.TemporaryDirectory() as directory:
            server = create_server({}, port=0, session_dir=directory)
            try:
                first, second = server.session_lock(sid), server.session_lock(other)
                self.assertIsNot(first, second)
                self.assertIs(first, server.session_lock(sid))
            finally:
                server.server_close()

    def test_queue_defaults_are_ten_workers_and_one_hour(self):
        self.assertEqual(AX_CONFIG['ax']['max_workers'], 10)
        self.assertEqual(AX_CONFIG['ax']['worker_queue_timeout_seconds'], 3600)


if __name__ == '__main__':
    unittest.main()

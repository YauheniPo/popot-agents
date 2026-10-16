"""Opt-in real AX capacity check, with deterministic turns and no model calls."""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ax_local.tests.test_session_lifecycle_live import LifecycleRunner
from popot_agents.orchestrator.main import create_server
from tests import test_chat as chat_tests


@unittest.skipUnless(os.getenv('AX_TEST_LIVE_QUEUE'), 'requires idle local AX/Substrate')
class LiveWorkerQueueTests(unittest.TestCase):
    def test_ten_suspended_workers_hold_slots_and_eleventh_starts_after_delete(self):
        root = Path(__file__).resolve().parents[2]
        image = (root / 'ax_local/.local/worker-image').read_text().strip()
        environment = {'PATH': str(root / 'ax_local/.local/bin') + os.pathsep + os.environ['PATH'],
                       'KUBECONFIG': str(root / 'ax_local/.local/kubeconfig')}
        with patch.dict(os.environ, environment), tempfile.TemporaryDirectory() as directory:
            runner = LifecycleRunner(image=image, model='unused', base_url='http://localhost:1/v1')
            def task_names():
                # The CLI table contains only metadata; never retrieve manifests/env.
                result = runner._run('get', 'tasks')
                return {line.split()[0] for line in result.stdout.splitlines()
                        if line.startswith('popot-chat-')}
            self.assertEqual(task_names(), set(), 'live capacity check requires no existing Popot Tasks')
            server = create_server({'test': runner}, port=0, session_dir=directory)
            replies, errors = [], []
            waiting = None
            try:
                def post(message):
                    return chat_tests.TaskSessionTests._post_to_handler(
                        server.RequestHandlerClass, server, {'agent': 'test', 'message': message})
                ids = []
                for index in range(10):
                    status, result = post('worker ' + str(index))
                    self.assertEqual(status, 200, result)
                    ids.append(result['sessionId'])
                    self.assertEqual(server.live[ids[-1]].worker.status, 'suspended')
                self.assertEqual(runner.worker_queue.status(), {'limit': 10, 'active': 10, 'queued': 0})
                def eleventh():
                    try:
                        replies.append(post('eleventh'))
                    except Exception as exc:
                        errors.append(exc)
                waiting = threading.Thread(target=eleventh)
                waiting.start()
                deadline = time.monotonic() + 10
                while runner.worker_queue.status()['queued'] != 1 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(runner.worker_queue.status()['queued'], 1)
                self.assertEqual(task_names(), {'popot-chat-' + sid for sid in ids})
                self.assertEqual(replies, [])

                # Exercise the same explicit-deletion handler used by HTTP.
                handler = server.RequestHandlerClass.__new__(server.RequestHandlerClass)
                handler.server = server
                deleted = []
                handler._reply = lambda status, result: deleted.append((status, result))
                with server.session_lock(ids[0]):
                    handler._delete_chat(ids[0])
                self.assertEqual(deleted, [(200, {'status': 'closed'})])
                deadline = time.monotonic() + 120
                while waiting.is_alive() and time.monotonic() < deadline:
                    server.service_actions()
                    waiting.join(0.2)
                self.assertFalse(waiting.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(len(replies), 1)
                status, result = replies[0]
                self.assertEqual(status, 200, result)
                self.assertEqual(result['answer'], 'fresh|eleventh')
                self.assertEqual(server.live[result['sessionId']].worker.status, 'suspended')
                self.assertEqual(runner.worker_queue.status(), {'limit': 10, 'active': 10, 'queued': 0})
                self.assertEqual(len(task_names()), 10)
                print('AX live queue: 10 suspended Tasks; eleventh waited and started after confirmed deletion')
            finally:
                runner.shutdown_worker_queue()
                if waiting is not None:
                    waiting.join(130)
                server.server_close()
                runner.maintenance()
            self.assertEqual(task_names(), set(), 'all live-check Tasks must be removed')


if __name__ == '__main__':
    unittest.main()

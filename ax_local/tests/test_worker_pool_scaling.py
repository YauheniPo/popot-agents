"""Elastic capacity must await registration and never shrink busy workers."""
import threading
import time
import unittest

from ax_local.api.worker_pool import WorkerPoolController
from ax_local.tests.test_worker_queue import wait_for


class Backend:
    def __init__(self):
        self.desired = self.ready = 3
        self.busy = set()
        self.scaled = []
        self.fail = False

    def inspect(self):
        if self.fail:
            raise OSError('inspection unavailable')
        return {'desired': self.desired, 'ready': self.ready, 'busy': set(self.busy)}

    def scale(self, count, snapshot):
        self.scaled.append(count)
        self.desired = count


class WorkerPoolScalingTests(unittest.TestCase):
    def setUp(self):
        self.backend = Backend()
        self.now = [0.0]
        self.pool = WorkerPoolController(self.backend, warm_workers=3, max_workers=30,
                                         idle_seconds=60, poll_seconds=.01,
                                         clock=lambda: self.now[0])
        self.pool.start()
        self.addCleanup(self.pool.close)

    def acquire(self, name, **kwargs):
        return self.pool.acquire(name, deadline=self.now[0]+300, **kwargs)

    def test_fourth_waits_for_registration_and_idle_pool_returns_to_three(self):
        for name in ('a', 'b', 'c'):
            self.acquire(name)
        done = threading.Event()
        thread = threading.Thread(target=lambda: (self.acquire('d'), done.set()))
        thread.start()
        self.addCleanup(thread.join, 2)
        wait_for(lambda: self.backend.desired == 4)
        self.assertFalse(done.is_set(), 'replica patch is not proof of readiness')
        self.backend.ready = 4
        self.assertTrue(done.wait(2))
        for name in ('a', 'b', 'c', 'd'):
            self.pool.release(name)
        self.now[0] = 59
        time.sleep(.04)
        self.assertEqual(self.backend.desired, 4)
        self.now[0] = 61
        wait_for(lambda: self.backend.desired == 3)

    def test_external_assignment_prevents_downscale_and_consumes_capacity(self):
        self.backend.desired = self.backend.ready = 5
        self.backend.busy = {'external'}
        self.now[0] = 100
        time.sleep(.05)
        self.assertEqual(self.backend.scaled, [])
        self.backend.busy.clear()
        wait_for(lambda: self.backend.desired == 3)

    def test_failed_inspection_never_shrinks_or_admits_using_stale_capacity(self):
        self.backend.desired = 5
        self.backend.fail = True
        wait_for(lambda: self.pool.status()['error'] == 'OSError')
        canceled = threading.Event()
        errors = []
        def run():
            try:
                self.acquire('waiting', cancel_event=canceled)
            except TimeoutError:
                errors.append('canceled')
        thread = threading.Thread(target=run)
        thread.start()
        self.now[0] = 100
        time.sleep(.05)
        self.assertEqual(self.backend.scaled, [])
        canceled.set()
        thread.join(2)
        self.assertEqual(errors, ['canceled'])
        self.assertEqual(self.pool.status()['pending'], 0)

    def test_deadline_removes_demand_and_shutdown_wakes_waiter(self):
        self.backend.ready = 0
        with self.pool.condition:
            self.pool.ready = 0
        errors = []
        def run():
            try:
                self.pool.acquire('waiting', deadline=2)
            except (TimeoutError, RuntimeError):
                errors.append('stopped')
        thread = threading.Thread(target=run)
        thread.start()
        wait_for(lambda: self.pool.status()['pending'] == 1)
        self.now[0] = 3
        thread.join(2)
        self.assertEqual(errors, ['stopped'])
        self.assertEqual(self.pool.status()['pending'], 0)
        self.now[0] = 0
        thread = threading.Thread(target=run)
        thread.start()
        wait_for(lambda: self.pool.status()['pending'] == 1)
        self.pool.close()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 2)

    def test_releasing_an_unknown_worker_is_a_no_op(self):
        self.pool.release('never-admitted')
        self.assertEqual(self.pool.status()['active'], 0)

    def test_close_without_start_does_not_join_a_thread(self):
        pool = WorkerPoolController(self.backend, warm_workers=3, max_workers=30,
                                    idle_seconds=60, poll_seconds=.01, clock=lambda: 0.0)
        pool.close()
        self.assertTrue(pool.closed)

    def test_repeated_demand_for_an_admitted_worker_is_idempotent(self):
        self.acquire('a')
        self.acquire('a')
        self.assertEqual(self.pool.status()['active'], 1)

    def test_duplicate_demand_for_a_waiting_worker_is_rejected(self):
        self.backend.ready = 0
        with self.pool.condition:
            self.pool.ready = 0
        def acquire_same():
            try:
                self.pool.acquire('same', deadline=2)
            except TimeoutError:
                pass
        thread = threading.Thread(target=acquire_same)
        thread.start()
        self.addCleanup(thread.join, 2)
        wait_for(lambda: self.pool.status()['pending'] == 1)
        with self.assertRaisesRegex(RuntimeError, 'duplicate AX worker demand'):
            self.acquire('same')
        self.now[0] = 3
        thread.join(2)


if __name__ == '__main__':
    unittest.main()

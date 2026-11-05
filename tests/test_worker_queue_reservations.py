import pytest

from ax_local.api.worker_queue import WorkerQueue
from popot_agents.orchestrator.main import AgentRunError, WorkerCapacityTimeout


def test_reserve_is_idempotent_for_the_same_branch():
    queue = WorkerQueue(max_workers=2, timeout_seconds=1)
    assert queue.reserve('session-a', 2) == 2
    assert queue.reserve('session-a', 1) == 2


def test_reserve_after_shutdown_is_rejected():
    queue = WorkerQueue(max_workers=2, timeout_seconds=1)
    queue.closed = True
    with pytest.raises(AgentRunError):
        queue.reserve('session-a', 1)


def test_adopt_reservation_requires_unused_reserved_capacity():
    queue = WorkerQueue(max_workers=2, timeout_seconds=1)
    queue.reservations['session-a'] = 0
    queue.active['worker-1'] = object()
    with pytest.raises(WorkerCapacityTimeout):
        queue.adopt_reservation('worker-1', 'session-a')


def test_adopt_reservation_is_a_noop_for_the_owning_branch():
    queue = WorkerQueue(max_workers=2, timeout_seconds=1)
    queue.groups['worker-1'] = 'session-a'
    queue.adopt_reservation('worker-1', 'session-a')
    assert queue.groups['worker-1'] == 'session-a'


def test_reserved_acquire_after_shutdown_is_rejected():
    queue = WorkerQueue(max_workers=2, timeout_seconds=1)
    queue.closed = True
    with pytest.raises(AgentRunError):
        queue.acquire('worker-1', reservation='session-a')


def test_reserved_acquire_respects_a_cancelled_start():
    queue = WorkerQueue(max_workers=2, timeout_seconds=1)
    cancelled = __import__('threading').Event()
    cancelled.set()
    with pytest.raises(TimeoutError):
        queue.acquire('worker-1', cancel_event=cancelled, reservation='session-a')

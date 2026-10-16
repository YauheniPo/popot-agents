"""FIFO reservations for local AX workers, including suspended sessions."""

from collections import deque
import threading
import time

from popot_agents.orchestrator.main import AgentRunError, WorkerCapacityTimeout


class WorkerQueue:
    def __init__(self, max_workers: int, timeout_seconds: float, *, clock=time.monotonic):
        self.max_workers = max_workers
        self.timeout_seconds = timeout_seconds
        self.clock = clock
        self.condition = threading.Condition()
        self.active = {}
        self.waiters = deque()
        self.closed = False

    def acquire(self, name: str, *, deadline: float | None = None, cancel_event=None):
        expires = self.clock() + self.timeout_seconds
        if deadline is not None:
            expires = min(expires, deadline)
        token = object()
        with self.condition:
            self.waiters.append(token)
            try:
                while True:
                    if self.closed:
                        raise AgentRunError('AX worker queue is shutting down')
                    if cancel_event is not None and cancel_event.is_set():
                        raise TimeoutError('AX worker startup canceled')
                    remaining = expires - self.clock()
                    if remaining <= 0:
                        if deadline is not None and deadline <= self.clock():
                            raise TimeoutError('AX startup deadline exceeded while queued')
                        raise WorkerCapacityTimeout(
                            f'AX worker queue timed out after {self.timeout_seconds:g} seconds; '
                            'retry when a worker slot is available')
                    if self.waiters[0] is token and len(self.active) < self.max_workers \
                            and name not in self.active:
                        self.active[name] = token
                        return token
                    # Cancellation events do not share this condition; poll
                    # them while releases and shutdown notify immediately.
                    self.condition.wait(min(remaining, 0.1) if cancel_event is not None else remaining)
            finally:
                self.waiters.remove(token)
                self.condition.notify_all()

    def release(self, name: str, token=None) -> None:
        with self.condition:
            if name in self.active and (token is None or self.active[name] is token):
                del self.active[name]
                self.condition.notify_all()

    def status(self) -> dict:
        with self.condition:
            return {'limit': self.max_workers, 'active': len(self.active),
                    'queued': len(self.waiters)}

    def close(self) -> None:
        with self.condition:
            self.closed = True
            self.condition.notify_all()

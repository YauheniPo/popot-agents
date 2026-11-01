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
        self.reservations = {}
        self.groups = {}

    def _used(self, key):
        return sum(name in self.active and group == key for name,group in self.groups.items())

    def _reserved(self):
        return sum(max(0, count-self._used(key)) for key,count in self.reservations.items())

    def reserve(self, key, slots):
        """Protect nested progress from unrelated sessions consuming spare slots."""
        with self.condition:
            if self.closed:
                raise AgentRunError('AX worker queue is shutting down')
            if key in self.reservations:
                return self.reservations[key]
            available = self.max_workers-len(self.active)-self._reserved()
            spare = 1 if self.max_workers >= 3 and available > 1 else 0
            count = min(slots, max(0,available-spare))
            self.reservations[key] = count
            return count

    def release_reservation(self, key):
        with self.condition:
            self.reservations.pop(key,None)
            self.condition.notify_all()

    def adopt_reservation(self, name, key):
        with self.condition:
            if self.groups.get(name) == key:
                return
            if name not in self.active or self._used(key) >= self.reservations.get(key,0):
                raise WorkerCapacityTimeout('No reserved nested worker capacity; finish or reconcile the current branch')
            self.groups[name] = key

    def acquire(self, name: str, *, deadline: float | None = None, cancel_event=None, reservation=None):
        expires = self.clock() + self.timeout_seconds
        if deadline is not None:
            expires = min(expires, deadline)
        token = object()
        with self.condition:
            if reservation is not None:
                if self.closed:
                    raise AgentRunError('AX worker queue is shutting down')
                if cancel_event is not None and cancel_event.is_set() or expires <= self.clock():
                    raise TimeoutError('AX reserved worker startup canceled or deadline exceeded')
                if name in self.active or self._used(reservation) >= self.reservations.get(reservation,0) or len(self.active) >= self.max_workers:
                    raise WorkerCapacityTimeout('Reserved branch capacity exhausted; no child worker started')
                self.active[name] = token
                self.groups[name] = reservation
                return token
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
                    if self.waiters[0] is token and len(self.active) + self._reserved() < self.max_workers \
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
                self.groups.pop(name,None)
                self.condition.notify_all()

    def status(self) -> dict:
        with self.condition:
            return {'limit': self.max_workers, 'active': len(self.active),
                    'queued': len(self.waiters)}

    def close(self) -> None:
        with self.condition:
            self.closed = True
            self.condition.notify_all()

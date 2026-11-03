"""Scale the local AX WorkerPool as active tasks need registered workers."""

import json
import subprocess
import threading
import time


class KubernetesWorkerPool:
    def __init__(self, context, namespace, name, kubeconfig, cli='kubectl-ate'):
        self.namespace, self.name = namespace, name
        self.flags = [f'--kubeconfig={kubeconfig}', f'--context={context}']
        self.cli = cli

    def _read(self, command):
        result = subprocess.run(command, text=True, capture_output=True, check=True, timeout=20)
        return json.loads(result.stdout)

    def inspect(self):
        kube = ['kubectl', *self.flags, '--request-timeout=15s', '-n', self.namespace]
        pool = self._read([*kube, 'get', 'workerpool', self.name, '-o', 'json'])
        pods = self._read([*kube, 'get', 'pods', '-l', f'ate.dev/worker-pool={self.name}', '-o', 'json'])['items']
        cli = [self.cli, *self.flags]
        workers = self._read([*cli, 'get', 'workers', '-n', self.namespace, '-o', 'json']).get('workers', [])
        actors = self._read([*cli, 'get', 'actors', '-A', '-o', 'json']).get('actors', [])
        ready = set()
        for pod in pods:
            meta, state = pod['metadata'], pod.get('status', {})
            if meta.get('deletionTimestamp') or not any(c.get('type') == 'Ready' and c.get('status') == 'True'
                    for c in state.get('conditions', [])):
                continue
            matches = [w for w in workers if w.get('workerPodUid') == meta['uid']
                       and w.get('workerPod') == meta['name']
                       and w.get('workerNamespace') == self.namespace
                       and w.get('workerPool') == self.name
                       and w.get('ip') == state.get('podIP')
                       and w.get('status', {}).get('state') == 'WORKER_STATE_ACTIVE']
            if len(matches) == 1:
                ready.add(meta['name'])
        busy = set()
        for actor in actors:
            assignment = actor.get('status', {}).get('workerAssignment') or {}
            if assignment.get('workerNamespace') == self.namespace and assignment.get('workerPool') == self.name:
                busy.add(actor['metadata']['name'])
        return {'desired': pool['spec']['replicas'], 'ready': len(ready), 'busy': busy,
                'resource_version': pool['metadata']['resourceVersion']}

    def scale(self, count, snapshot):
        patch = {'metadata': {'resourceVersion': snapshot['resource_version']},
                 'spec': {'replicas': count}}
        subprocess.run(['kubectl', *self.flags, '--request-timeout=15s', '-n', self.namespace,
                        'patch', 'workerpool', self.name, '--type=merge', '-p', json.dumps(patch)],
                       text=True, capture_output=True, check=True, timeout=20)


class WorkerPoolController:
    def __init__(self, backend, *, warm_workers, max_workers, idle_seconds,
                 poll_seconds=1, clock=time.monotonic):
        self.backend = backend
        self.warm_workers, self.max_workers = warm_workers, max_workers
        self.idle_seconds, self.poll_seconds, self.clock = idle_seconds, poll_seconds, clock
        self.condition = threading.Condition()
        self.pending = {}
        self.admitted = set()
        self.ready = 0
        self.last_demand = clock()
        self.closed = False
        self.thread = None
        self.error = None

    def start(self):
        self.thread = threading.Thread(target=self._loop, name='ax-worker-pool', daemon=True)
        self.thread.start()

    def _loop(self):
        while True:
            with self.condition:
                if self.closed:
                    return
                try:
                    snapshot = self.backend.inspect()
                    external = snapshot['busy'] - self.admitted
                    self.ready = max(0, snapshot['ready'] - len(external))
                    target = min(self.max_workers, max(self.warm_workers,
                                 len(self.pending) + len(self.admitted) + len(external)))
                    if target > snapshot['desired']:
                        self.backend.scale(target, snapshot)
                    elif not self.pending and not self.admitted and not snapshot['busy'] \
                            and self.clock() - self.last_demand >= self.idle_seconds \
                            and snapshot['desired'] > self.warm_workers:
                        self.backend.scale(self.warm_workers, snapshot)
                    self.error = None
                except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                    self.ready = 0
                    self.error = type(exc).__name__
                self.condition.notify_all()
                self.condition.wait(self.poll_seconds)

    def acquire(self, name, *, deadline, cancel_event=None):
        with self.condition:
            if name in self.admitted:
                return
            if name in self.pending:
                raise RuntimeError('duplicate AX worker demand')
            ticket = object()
            self.pending[name] = ticket
            self.last_demand = self.clock()
            self.condition.notify_all()
            try:
                while True:
                    if self.closed:
                        raise RuntimeError('AX worker pool is shutting down')
                    if cancel_event is not None and cancel_event.is_set():
                        raise TimeoutError('AX worker startup canceled')
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        raise TimeoutError('AX worker pool readiness deadline exceeded')
                    if next(iter(self.pending.values())) is ticket and len(self.admitted) < self.ready:
                        self.admitted.add(name)
                        self.pending.pop(name)
                        self.condition.notify_all()
                        return
                    self.condition.wait(min(remaining, .1))
            finally:
                if self.pending.get(name) is ticket:
                    self.pending.pop(name)
                    self.condition.notify_all()

    def release(self, name):
        with self.condition:
            if name in self.admitted:
                self.admitted.remove(name)
                self.ready = 0  # Wait for registry to confirm the old assignment is gone.
                self.last_demand = self.clock()
                self.condition.notify_all()

    def status(self):
        with self.condition:
            return {'active': len(self.admitted), 'pending': len(self.pending),
                    'ready': self.ready, 'error': self.error}

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        if self.thread:
            self.thread.join(25)

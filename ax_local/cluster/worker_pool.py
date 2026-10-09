"""Migrate the local demo compute pool without deleting AX sessions."""

from __future__ import annotations

import copy
import json
import re
import subprocess
import time
from pathlib import Path


SOURCE_UID = 'popot.dev/source-pool-uid'


def ensure_named_pool(kubeconfig: Path, cli: Path, context: str, namespace: str,
                      pool_name: str, timeout_seconds: int, *, run=subprocess.run,
                      clock=time.monotonic, sleep=time.sleep) -> None:
    if not re.fullmatch(r'kind-[a-z0-9-]+', context):
        raise ValueError('Worker pool migration requires a local kind context')
    for value in (namespace, pool_name):
        if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', value):
            raise ValueError('Invalid worker namespace or pool name')
    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        raise ValueError('Worker pool migration timeout must be positive')
    flags = [f'--kubeconfig={kubeconfig}', f'--context={context}']
    kube = ['kubectl', *flags, '--request-timeout=15s', '-n', namespace]
    deadline = clock() + timeout_seconds

    def execute(args, **kw):
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError('Local worker pool migration timed out')
        return run(args, text=True, capture_output=True, check=True,
                   timeout=min(30, remaining), **kw)

    while clock() < deadline:
        try:
            pools = json.loads(execute([*kube, 'get', 'workerpools', '-o', 'json']).stdout)['items']
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            # The kind API may still be recovering just after Docker starts its node.
            sleep(min(2, max(0, deadline - clock())))
            continue
        old = next((p for p in pools if p['metadata']['name'] == 'counter'), None)
        new = next((p for p in pools if p['metadata']['name'] == pool_name), None)
        if pool_name == 'counter' or old is None:
            if new is None:
                raise ValueError(f'Configure a WorkerPool named {pool_name} in {namespace}')
            return
        if old['metadata'].get('deletionTimestamp'):
            sleep(min(2, max(0, deadline - clock())))
            continue
        if new is None:
            metadata = {'name': pool_name, 'namespace': namespace,
                        'labels': {'workload': pool_name},
                        'annotations': {SOURCE_UID: old['metadata']['uid']}}
            manifest = {'apiVersion': old['apiVersion'], 'kind': 'WorkerPool',
                        'metadata': metadata, 'spec': copy.deepcopy(old['spec'])}
            print(f'Creating local AX worker pool {namespace}/{pool_name}', flush=True)
            execute([*kube, 'create', '-f', '-'], input=json.dumps(manifest))
            continue
        if (new['metadata'].get('annotations', {}).get(SOURCE_UID) != old['metadata']['uid']
                or new['spec'] != old['spec']):
            raise ValueError('Existing named pool differs from the legacy pool; migration stopped')

        pods = json.loads(execute([*kube, 'get', 'pods', '-l',
                                  f'ate.dev/worker-pool={pool_name}', '-o', 'json']).stdout)['items']
        workers = json.loads(execute([str(cli), *flags, 'get', 'workers', '-n', namespace,
                                     '-o', 'json']).stdout).get('workers', [])
        desired = new['spec']['replicas']
        if type(desired) is not int or desired <= 0:
            raise ValueError('Worker pool migration requires positive replicas')
        ready = len(pods) == desired
        for pod in pods:
            meta, status = pod['metadata'], pod.get('status', {})
            matches = [w for w in workers if w.get('workerPool') == pool_name
                       and w.get('workerNamespace') == namespace
                       and w.get('workerPodUid') == meta['uid']
                       and w.get('workerPod') == meta['name']
                       and w.get('ip') == status.get('podIP')
                       and w.get('status', {}).get('state') == 'WORKER_STATE_ACTIVE']
            ready = ready and bool(status.get('podIP')) and not meta.get('deletionTimestamp') and any(
                c.get('type') == 'Ready' and c.get('status') == 'True'
                for c in status.get('conditions', [])) and len(matches) == 1
        if ready:
            # Workers are shared by all atespaces, including internal golden actors.
            actors = json.loads(execute([str(cli), *flags, 'get', 'actors', '-A',
                                        '-o', 'json']).stdout).get('actors', [])
            busy = any(a.get('status', {}).get('workerAssignment', {}).get('workerNamespace') == namespace
                       and a['status']['workerAssignment'].get('workerPool') == 'counter'
                       for a in actors)
            if not busy:
                meta = old['metadata']
                options = {'apiVersion': 'v1', 'kind': 'DeleteOptions',
                           'propagationPolicy': 'Foreground',
                           'preconditions': {'uid': meta['uid'], 'resourceVersion': meta['resourceVersion']}}
                print(f'Removing idle legacy worker pool {namespace}/counter', flush=True)
                execute([*kube, 'delete', '--raw',
                         f'/apis/ate.dev/v1alpha1/namespaces/{namespace}/workerpools/counter',
                         '-f', '-'], input=json.dumps(options))
            else:
                print('Waiting for actors to release the legacy counter pool', flush=True)
        sleep(min(2, max(0, deadline - clock())))
    raise TimeoutError('Local worker pool migration timed out; legacy pool was retained if busy')

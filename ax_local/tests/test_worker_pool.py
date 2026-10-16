"""A local pool rename keeps old capacity until the replacement is ready."""

import copy
import json
import subprocess
import unittest
from pathlib import Path

from ax_local.cluster.worker_pool import ensure_named_pool


NAMESPACE = 'ate-demo-counter'


def pool(name='counter'):
    return {'apiVersion': 'ate.dev/v1alpha1', 'kind': 'WorkerPool',
            'metadata': {'name': name, 'namespace': NAMESPACE, 'uid': 'old-pool',
                         'resourceVersion': '12'},
            'spec': {'replicas': 3, 'sandboxClass': 'gvisor', 'workerImage': 'pinned-image',
                     'template': {'nodeSelector': {'ate.dev/substrate-version': 'pinned'},
                                  'resources': {'limits': {'memory': '1536Mi'}}}}}


class WorkerPoolTests(unittest.TestCase):
    def migrate(self, *, ready=True, active=False, present=False, conflict=False,
                context='kind-popot-ax', fail_read_once=False):
        self.calls = []
        self.tick = 0
        old = pool()
        pools = [old] if not present else [pool('popot-workers')]
        new = None
        failed = False

        def run(args, **kw):
            nonlocal new, pools, failed
            self.calls.append((args, kw))
            self.assertLessEqual(kw['timeout'], 10)
            if 'workerpools' in args and fail_read_once and not failed:
                failed = True
                raise subprocess.CalledProcessError(1, args)
            if 'create' in args:
                new = json.loads(kw['input'])
                new['metadata'].update(uid='new-pool', resourceVersion='13')
                pools.append(new)
                data = new
            elif 'delete' in args:
                if conflict:
                    raise subprocess.CalledProcessError(1, args)
                pools.remove(old)
                data = {}
            elif 'workerpools' in args:
                data = {'items': copy.deepcopy(pools)}
            elif 'pods' in args:
                data = {'items': [{'metadata': {'name': f'popot-workers-{i}', 'uid': f'u{i}',
                                                'namespace': NAMESPACE},
                                   'status': {'podIP': f'10.0.0.{i}', 'conditions': [
                                       {'type': 'Ready', 'status': 'True' if ready else 'False'}]}}
                                  for i in range(3)]}
            elif 'workers' in args:
                data = {'workers': [{'workerNamespace': NAMESPACE, 'workerPool': 'popot-workers',
                                     'workerPod': f'popot-workers-{i}', 'workerPodUid': f'u{i}',
                                     'ip': f'10.0.0.{i}', 'status': {'state': 'WORKER_STATE_ACTIVE'}}
                                    for i in range(3)]}
            else:
                data = {'actors': [{'status': {'state': 'ACTOR_STATE_RUNNING',
                                               'workerAssignment': {'workerNamespace': NAMESPACE,
                                                                    'workerPool': 'counter'}}}]
                        if active else []}
            return subprocess.CompletedProcess(args, 0, json.dumps(data))

        def sleep(seconds):
            self.tick += seconds

        ensure_named_pool(Path('config'), Path('kubectl-ate'), context, NAMESPACE,
                          'popot-workers', 10, run=run, clock=lambda: self.tick, sleep=sleep)
        return new

    def test_copies_spec_and_deletes_legacy_only_after_replacement_ready(self):
        new = self.migrate()
        self.assertEqual(new['spec'], pool()['spec'])
        self.assertEqual(new['metadata']['name'], 'popot-workers')
        delete = next((args, kw) for args, kw in self.calls if 'delete' in args)
        self.assertIn('/apis/ate.dev/v1alpha1/namespaces/ate-demo-counter/workerpools/counter', delete[0])
        self.assertEqual(json.loads(delete[1]['input'])['preconditions'],
                         {'uid': 'old-pool', 'resourceVersion': '12'})
        actions = [args for args, _ in self.calls]
        self.assertLess(next(i for i, a in enumerate(actions) if 'workers' in a),
                        next(i for i, a in enumerate(actions) if 'delete' in a))

    def test_unready_replacement_never_deletes_old_pool(self):
        with self.assertRaises(TimeoutError):
            self.migrate(ready=False)
        self.assertFalse(any('delete' in args for args, _ in self.calls))

    def test_active_actor_in_any_atespace_prevents_legacy_deletion(self):
        with self.assertRaises(TimeoutError):
            self.migrate(active=True)
        self.assertFalse(any('delete' in args for args, _ in self.calls))
        self.assertTrue(any('actors' in args and '-A' in args for args, _ in self.calls))

    def test_repeat_launch_keeps_new_pool_unchanged(self):
        self.migrate(present=True)
        self.assertFalse(any('create' in args or 'delete' in args for args, _ in self.calls))

    def test_waits_for_kubernetes_after_local_docker_start(self):
        self.migrate(present=True, fail_read_once=True)
        self.assertGreater(self.tick, 0)
        self.assertFalse(any('create' in args or 'delete' in args for args, _ in self.calls))

    def test_identity_conflict_is_not_retried_as_a_blind_delete(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.migrate(conflict=True)
        self.assertEqual(sum('delete' in args for args, _ in self.calls), 1)

    def test_nonlocal_context_is_rejected_before_mutations(self):
        with self.assertRaises(ValueError):
            self.migrate(context='production')
        self.assertEqual(self.calls, [])

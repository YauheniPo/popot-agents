"""The Kubernetes AX adapter reads the pool, pod, worker and actor snapshots."""

import json
import subprocess
import unittest
from unittest import mock

from ax_local.api.worker_pool import KubernetesWorkerPool


NAMESPACE = 'ate-demo-counter'


def completed(command, payload):
    return subprocess.CompletedProcess(command, 0, json.dumps(payload))


class KubernetesWorkerPoolTests(unittest.TestCase):
    def backend(self, *, pod_ready=True, worker_ip='10.0.0.1'):
        self.calls = []

        def run(command, **kw):
            self.calls.append((command, kw))
            self.assertEqual(kw['timeout'], 20)
            if 'workerpool' in command:
                return completed(command, {'metadata': {'resourceVersion': '42'},
                                           'spec': {'replicas': 3}})
            if 'pods' in command:
                return completed(command, {'items': [
                    {'metadata': {'name': 'popot-workers-0', 'uid': 'u0'},
                     'status': {'podIP': '10.0.0.1', 'conditions': [
                         {'type': 'Ready', 'status': 'True' if pod_ready else 'False'}]}}]})
            if '-A' in command:
                return completed(command, {'actors': [
                    {'metadata': {'name': 'actor-1'},
                     'status': {'workerAssignment': {'workerNamespace': NAMESPACE,
                                                     'workerPool': 'counter'}}}]})
            return completed(command, {'workers': [
                {'workerPodUid': 'u0', 'workerPod': 'popot-workers-0',
                 'workerNamespace': NAMESPACE, 'workerPool': 'counter',
                 'ip': worker_ip, 'status': {'state': 'WORKER_STATE_ACTIVE'}}]})

        return KubernetesWorkerPool('kind-popot-ax', NAMESPACE, 'counter',
                                    '/tmp/kubeconfig'), run

    def test_inspect_counts_ready_registered_workers_and_busy_actors(self):
        backend, run = self.backend()
        with mock.patch('subprocess.run', side_effect=run):
            snapshot = backend.inspect()
        self.assertEqual(snapshot, {'desired': 3, 'ready': 1, 'busy': {'actor-1'},
                                    'resource_version': '42'})
        names = [command[0] for command, _ in self.calls]
        self.assertEqual(names, ['kubectl', 'kubectl', 'kubectl-ate', 'kubectl-ate'])
        self.assertTrue(all('--kubeconfig=/tmp/kubeconfig' in command for command, _ in self.calls))
        self.assertIn('-A', self.calls[-1][0])

    def test_inspect_ignores_unready_pods_and_workers_with_another_ip(self):
        backend, run = self.backend(pod_ready=False)
        with mock.patch('subprocess.run', side_effect=run):
            self.assertEqual(backend.inspect()['ready'], 0)
        backend, run = self.backend(worker_ip='10.0.0.9')
        with mock.patch('subprocess.run', side_effect=run):
            self.assertEqual(backend.inspect()['ready'], 0)

    def test_scale_patches_replicas_and_guards_the_resource_version(self):
        backend, run = self.backend()
        with mock.patch('subprocess.run', side_effect=run):
            backend.scale(5, {'resource_version': '42'})
        command, kw = self.calls[-1]
        self.assertEqual(command[:3], ['kubectl', '--kubeconfig=/tmp/kubeconfig', '--context=kind-popot-ax'])
        self.assertIn('patch', command)
        self.assertEqual(json.loads(command[command.index('-p') + 1]),
                         {'metadata': {'resourceVersion': '42'}, 'spec': {'replicas': 5}})
        self.assertEqual(kw['timeout'], 20)

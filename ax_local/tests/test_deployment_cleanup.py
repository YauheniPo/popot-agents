"""Only disposable objects in the selected local deployment may be removed."""

import json
import subprocess
import unittest
from pathlib import Path

from ax_local.cluster.deployment_cleanup import cleanup_cluster, cleanup_docker


def obj(name, kind, owner=None, *, revision='1', replicas=0, status=None):
    metadata = {'name': name, 'namespace': 'ax-system', 'uid': name + '-uid',
                'resourceVersion': '10', 'annotations': {'deployment.kubernetes.io/revision': revision}}
    if owner:
        metadata['ownerReferences'] = [{'kind': 'Deployment', 'name': 'ax-server',
                                        'uid': owner, 'controller': True}]
    return {'kind': kind, 'metadata': metadata, 'spec': {'replicas': replicas}, 'status': status or {}}


class DeploymentCleanupTests(unittest.TestCase):
    def test_retains_current_and_one_rollback_rs_and_live_jobs_pods(self):
        items = [obj('ax-server', 'Deployment', replicas=1),
                 obj('oldest', 'ReplicaSet', 'ax-server-uid', revision='1'),
                 obj('rollback', 'ReplicaSet', 'ax-server-uid', revision='2'),
                 obj('current', 'ReplicaSet', 'ax-server-uid', revision='3', replicas=1),
                 obj('unowned', 'ReplicaSet'),
                 obj('active-job', 'Job', status={'active': 1}),
                 obj('done-job', 'Job', status={'conditions': [{'type': 'Complete', 'status': 'True'}]}),
                 obj('running', 'Pod', status={'phase': 'Running'}),
                 obj('finished', 'Pod', status={'phase': 'Succeeded'})]
        calls = []

        def run(args, **kw):
            calls.append((args, kw))
            if 'get' in args:
                return subprocess.CompletedProcess(args, 0, json.dumps({'items': items}))
            return subprocess.CompletedProcess(args, 0, '{}')

        count = cleanup_cluster(Path('config'), 'kind-popot-ax', ['ax-system'], run=run)
        self.assertEqual(count, 3)
        deletes = [(args, kw) for args, kw in calls if 'delete' in args]
        self.assertEqual({a[a.index('--raw')+1].split('/')[-1] for a, _ in deletes},
                         {'oldest', 'done-job', 'finished'})
        for args, kw in deletes:
            name = args[args.index('--raw')+1].split('/')[-1]
            self.assertEqual(json.loads(kw['input'])['preconditions'],
                             {'uid': name + '-uid', 'resourceVersion': '10'})

    def test_scaled_down_deployment_keeps_its_latest_rs_and_rollback(self):
        items = [obj('ax-server', 'Deployment'),
                 *(obj(f'rs{i}', 'ReplicaSet', 'ax-server-uid', revision=str(i)) for i in range(1,4))]
        deletes = []
        def run(args, **kw):
            if 'delete' in args:
                deletes.append(args)
            return subprocess.CompletedProcess(args, 0, json.dumps({'items': items}))
        cleanup_cluster(Path('config'), 'kind-popot-ax', ['ax-system'], run=run)
        self.assertEqual(len(deletes), 1)
        self.assertTrue(deletes[0][deletes[0].index('--raw')+1].endswith('/rs1'))

    def test_docker_removes_only_stopped_containers_of_this_checkout_and_dangling_project_images(self):
        calls = []
        def container(cid, *, project='popot-ax-local', running=False, directory='/repo/ax_local'):
            return {'Id': cid, 'State': {'Running': running, 'Status': 'running' if running else 'exited'},
                    'Config': {'Labels': {'com.docker.compose.project': project,
                                          'com.docker.compose.project.working_dir': directory}}}
        containers = [container('old'), container('live', running=True),
                      container('foreign', project='another'), container('other-checkout', directory='/elsewhere')]
        def run(args, **kw):
            calls.append(args)
            if args[:3] == ['docker', 'ps', '-aq']:
                data = '\n'.join(c['Id'] for c in containers)
            elif args[:2] == ['docker', 'inspect']:
                data = json.dumps([next(c for c in containers if c['Id'] == args[2])])
            else:
                data = ''
            return subprocess.CompletedProcess(args, 0, data)
        cleanup_docker(Path('/repo'), run=run)
        self.assertIn(['docker', 'rm', 'old'], calls)
        self.assertEqual([a for a in calls if a[:2] == ['docker', 'rm']], [['docker', 'rm', 'old']])
        self.assertIn(['docker', 'image', 'prune', '--force', '--filter',
                       'label=com.popot.project=popot-agents'], calls)
        self.assertFalse(any('--all' in a or '-a' in a for a in calls))
        self.assertFalse(any('--force' in a for a in calls if a[:2] == ['docker', 'rm']))

    def test_completed_job_owns_its_terminal_pods_until_cascade_deletion(self):
        job = obj('complete', 'Job', status={'conditions': [{'type': 'Complete', 'status': 'True'}]})
        pod = obj('completed-pod', 'Pod', status={'phase': 'Succeeded'})
        pod['metadata']['ownerReferences'] = [{'kind': 'Job', 'uid': 'complete-uid', 'controller': True}]
        deletes = []
        def run(args, **kw):
            if 'delete' in args:
                deletes.append(args)
            return subprocess.CompletedProcess(args, 0, json.dumps({'items': [job, pod]}))
        cleanup_cluster(Path('config'), 'kind-popot-ax', ['ax-system'], run=run)
        self.assertEqual(len(deletes), 1)
        self.assertTrue(deletes[0][deletes[0].index('--raw')+1].endswith('/jobs/complete'))

    def test_refuses_other_context_before_querying_cluster(self):
        with self.assertRaises(ValueError):
            cleanup_cluster(Path('config'), 'production', ['ax-system'],
                            run=lambda *a, **kw: self.fail('unexpected command'))

import json
import io
import os
import subprocess
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch
from pathlib import Path
import tempfile

from ax_local.cluster.holmes import IMAGE, NAMESPACE, manifests, investigation_job, scope, wait_for_pod, ensure_image, main


CONFIG = {'context': 'kind-popot-ax', 'atespace': 'ate-demo-counter'}
TEST_MODEL = 'selected-free-combo'


class HolmesTests(unittest.TestCase):
    def setUp(self):
        bundle = patch('ax_local.cluster.holmes.omniroute_ca_bundle', return_value='public-ca-bundle')
        bundle.start()
        self.addCleanup(bundle.stop)

    def test_job_and_runner_config_follow_service_settings(self):
        root=Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)
            (target/'config').mkdir()
            (target/'ax_local/holmes').mkdir(parents=True)
            (target/'ax_local/holmes/runner.py').write_text('runner')
            settings=json.loads((root/'config/holmes.json').read_text())
            settings.update(active_deadline_seconds=180,ttl_seconds_after_finished=3600,max_steps=5)
            settings['resources']['limits']['memory']='1Gi'
            (target/'config/holmes.json').write_text(json.dumps(settings))
            with patch('ax_local.cluster.holmes.ROOT',target):
                job=investigation_job('Check pods',TEST_MODEL)
                data=next(o for o in manifests(CONFIG) if o['kind']=='ConfigMap')['data']
        self.assertEqual(job['spec']['activeDeadlineSeconds'],180)
        self.assertEqual(job['spec']['ttlSecondsAfterFinished'],3600)
        self.assertEqual(job['spec']['template']['spec']['containers'][0]['resources']['limits']['memory'],'1Gi')
        self.assertEqual(json.loads(data['settings.json'])['max_steps'],5)
        self.assertEqual(data['ca-bundle.pem'], 'public-ca-bundle')

    def test_model_is_required_before_any_cluster_or_image_actions(self):
        for value in (None, '', '   '):
            environment = {'OMNIROUTE_API_KEY': 'test-gateway-key'}
            if value is not None:
                environment['HOLMES_MODEL'] = value
            with self.subTest(value=value), patch.dict(os.environ, environment, clear=True), \
                 patch('sys.argv', ['holmes', 'Check AX']), \
                 patch('ax_local.cluster.holmes.load_dotenv'), \
                 patch('ax_local.cluster.holmes.subprocess.run',
                       side_effect=AssertionError('Cluster access before HOLMES_MODEL validation')) as execute, \
                 patch('ax_local.cluster.holmes.ensure_image') as image:
                with self.assertRaisesRegex(ValueError, 'HOLMES_MODEL.*\\.env'):
                    main()
                execute.assert_not_called()
                image.assert_not_called()

    def test_long_diagnosis_logs_keep_streaming_and_key_only_uses_stdin(self):
        def run(args, **kwargs):
            if 'nodes' in args:
                output = {'items': [{'metadata': {'name': 'popot-ax-control-plane'},
                                     'status': {'nodeInfo': {'architecture': 'arm64'}}}]}
            elif 'create' in args:
                output = {'metadata': {'name': 'test-job'}}
            else:
                output = {}
            return subprocess.CompletedProcess(args, 0, json.dumps(output))

        with patch.dict(os.environ, {'OMNIROUTE_API_KEY': 'private-gateway-key', 'HOLMES_MODEL': TEST_MODEL}), \
             patch('sys.argv', ['holmes', 'Check AX']), \
             patch('ax_local.cluster.holmes.load_dotenv'), \
             patch('ax_local.cluster.holmes.manifests', return_value=[]), \
             patch('ax_local.cluster.holmes.ensure_image'), \
             patch('ax_local.cluster.holmes.wait_for_pod', return_value='test-pod'), \
             patch('ax_local.cluster.holmes.subprocess.run', side_effect=run) as execute, \
             redirect_stdout(io.StringIO()):
            main()
        calls = execute.call_args_list
        log_call = next(call for call in calls if 'logs' in call.args[0])
        self.assertNotIn('--request-timeout=30s', log_call.args[0])
        self.assertEqual(log_call.kwargs['timeout'], 660)
        self.assertNotIn('private-gateway-key', json.dumps([call.args[0] for call in calls]))
        secret = next(json.loads(call.kwargs['input']) for call in calls
                      if call.kwargs.get('input') and 'Secret' in call.kwargs['input'])
        self.assertEqual(secret['stringData']['api-key'], 'private-gateway-key')
        job = next(json.loads(call.kwargs['input']) for call in calls
                   if 'create' in call.args[0])
        env = {item['name']: item.get('value') for item in job['spec']['template']['spec']['containers'][0]['env']}
        self.assertEqual(env['HOLMES_MODEL'], TEST_MODEL)

    def test_job_keeps_home_and_temporary_files_on_its_private_volume(self):
        pod = investigation_job('Check AX', TEST_MODEL)['spec']['template']['spec']
        container = pod['containers'][0]
        env = {item['name']: item['value'] for item in container['env'] if 'value' in item}
        self.assertEqual(env['HOME'], '/home/holmes')
        self.assertEqual(env['TMPDIR'], env['HOME'])
        self.assertEqual(env['SSL_CERT_FILE'], '/etc/popot-holmes/ca-bundle.pem')
        self.assertEqual(env['REQUESTS_CA_BUNDLE'], env['SSL_CERT_FILE'])
        mounts = {item['mountPath']: item['name'] for item in container['volumeMounts']}
        self.assertNotIn('/tmp', mounts)
        volume = next(item for item in pod['volumes'] if item['name'] == mounts[env['HOME']])
        self.assertEqual(volume['emptyDir'], {'sizeLimit': '512Mi'})
        self.assertEqual(pod['securityContext']['runAsUser'], 10001)
        self.assertEqual(pod['securityContext']['fsGroup'], 10001)
        self.assertTrue(container['securityContext']['readOnlyRootFilesystem'])

    def test_cached_kind_image_does_not_trigger_a_download(self):
        with patch('ax_local.cluster.holmes.subprocess.run') as run:
            run.return_value.stdout = 'docker.io/' + IMAGE + '\n'
            ensure_image('popot-ax-control-plane', 'arm64')
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][:3], ['docker', 'exec', 'popot-ax-control-plane'])

    def test_pending_pod_has_a_finite_startup_deadline(self):
        kube = Mock(return_value=json.dumps({'items': []}))
        with patch('ax_local.cluster.holmes.time.monotonic', side_effect=[0, 0, 301]), \
             patch('ax_local.cluster.holmes.time.sleep'):
            with self.assertRaises(TimeoutError):
                wait_for_pod(kube, 'test-job')

    def test_image_pull_finishes_before_logs_are_read(self):
        kube = Mock(side_effect=[json.dumps({'items': [{'metadata': {'name': 'test-pod'},
                                                       'status': {'phase': phase}}]})
                                 for phase in ('Pending', 'Running')])
        with patch('ax_local.cluster.holmes.time.sleep') as sleep:
            self.assertEqual(wait_for_pod(kube, 'test-job'), 'test-pod')
        sleep.assert_called_once_with(2)
        self.assertEqual(kube.call_count, 2)

    def test_failed_job_is_not_reported_ready(self):
        kube = Mock(return_value=json.dumps({'items': [{'metadata': {'name': 'failed-pod'},
                                                        'status': {'phase': 'Failed'}}]}))
        with self.assertRaisesRegex(RuntimeError, 'failed-pod'):
            wait_for_pod(kube, 'failed-job')

    def test_scope_rejects_remote_context_and_invalid_namespace(self):
        self.assertEqual(scope(CONFIG), ('ax-system', 'ate-system', 'ate-demo-counter',
                                        'otel-system', 'ax-diagnostics', 'kube-system'))
        for config in ({**CONFIG, 'context': 'production'},
                       {**CONFIG, 'atespace': '../production'}):
            with self.assertRaises(ValueError):
                scope(config)

    def test_permissions_only_read_ax_pods_logs_and_events(self):
        objects = manifests(CONFIG)
        roles = [o for o in objects if o['kind'] == 'Role']
        self.assertEqual({o['metadata']['namespace'] for o in roles}, set(scope(CONFIG)))
        self.assertFalse(any(o['kind'].startswith('ClusterRole') for o in objects))
        for role in roles:
            self.assertEqual(role['rules'], [{
                'apiGroups': [''], 'resources': ['pods', 'pods/log', 'events'],
                'verbs': ['get', 'list', 'watch']}])
        for binding in (o for o in objects if o['kind'] == 'RoleBinding'):
            self.assertEqual(binding['subjects'], [{
                'kind': 'ServiceAccount', 'name': 'holmes', 'namespace': NAMESPACE}])

    def test_kubectl_uses_the_pod_service_account(self):
        configmap = next(o for o in manifests(CONFIG) if o['kind'] == 'ConfigMap')
        kubeconfig = json.loads(configmap['data']['kubeconfig.json'])
        self.assertEqual(kubeconfig['clusters'][0]['cluster']['server'], 'https://kubernetes.default.svc')
        user = kubeconfig['users'][0]['user']
        self.assertEqual(user, {'tokenFile': '/var/run/secrets/kubernetes.io/serviceaccount/token'})
        container = investigation_job('Check AX', TEST_MODEL)['spec']['template']['spec']['containers'][0]
        env = {entry['name']: entry for entry in container['env']}
        self.assertEqual(env['KUBECONFIG']['value'], '/etc/popot-holmes/kubeconfig.json')

    def test_tools_are_explicitly_namespaced_without_shell_or_external_sources(self):
        configmap = next(o for o in manifests(CONFIG) if o['kind'] == 'ConfigMap')
        toolsets = json.loads(configmap['data']['tools.json'])
        self.assertEqual(set(toolsets), {'ax/pods'})
        tools = toolsets['ax/pods']['tools']
        self.assertEqual({t['name'] for t in tools}, {
            'ax_pods', 'ax_events', 'ax_pod_logs', 'ax_previous_pod_logs'})
        for tool in tools:
            self.assertTrue(tool['command'].startswith('kubectl --request-timeout=20s -n {{ namespace }} '))
            self.assertEqual(tool['parameters']['namespace']['enum'], list(scope(CONFIG)))
            self.assertNotIn('--all-namespaces', tool['command'])
            self.assertNotIn('script', tool)

    def test_pod_listing_omits_the_large_pod_spec_that_overflows_context(self):
        configmap = next(o for o in manifests(CONFIG) if o['kind'] == 'ConfigMap')
        tools = json.loads(configmap['data']['tools.json'])['ax/pods']['tools']
        command = next(t['command'] for t in tools if t['name'] == 'ax_pods')
        self.assertIn('custom-columns=', command)
        for field in ('restartCount', 'lastState.terminated.reason', 'resources.limits.memory'):
            self.assertIn(field, command)
        self.assertNotIn('-o json', command)

    def test_job_uses_pinned_model_and_secret_without_host_credentials(self):
        job = investigation_job('Check AX pods', TEST_MODEL)
        self.assertEqual(job['metadata']['namespace'], NAMESPACE)
        self.assertEqual(job['spec']['backoffLimit'], 0)
        self.assertEqual(job['spec']['activeDeadlineSeconds'], 600)
        pod = job['spec']['template']['spec']
        self.assertEqual(pod['serviceAccountName'], 'holmes')
        container = pod['containers'][0]
        self.assertEqual(container['image'], IMAGE)
        env = {entry['name']: entry for entry in container['env']}
        self.assertEqual(env['HOLMES_MODEL']['value'], TEST_MODEL)
        self.assertEqual(env['OPENAI_API_KEY']['valueFrom']['secretKeyRef'], {
            'name': 'holmes-omniroute', 'key': 'api-key'})
        self.assertFalse(any('hostPath' in volume for volume in pod['volumes']))
        self.assertNotIn('ax_local/.local', json.dumps(job))


if __name__ == '__main__':
    unittest.main()

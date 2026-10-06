import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import shutil
from unittest.mock import patch

from ax_local.cluster import substrate_cleanup
from ax_local.cluster.substrate_cleanup import patch_terminate, daemonset_patch


CALL = '\tif _, err := client.TerminateWorkload(ctx, &ateompb.TerminateWorkloadRequest{'
END = '\n\t// Deregister after teardown succeeds'


class SubstrateCleanupTests(unittest.TestCase):
    def test_retry_guard_wraps_only_termination_rpc(self):
        source = 'before\n' + CALL + '\n\t}); err != nil {\n\t\treturn nil, err\n\t}\n' + END + '\nafter\n'
        patched = patch_terminate(source)
        self.assertIn('popotWorkloadWasReset', patched)
        self.assertIn('if !alreadyReset {', patched)
        self.assertIn('atev1alpha1.SandboxClassGvisor', patched)
        self.assertIn('return nil, err', patched)
        self.assertTrue(patched.endswith(END + '\nafter\n'))
        self.assertEqual(patch_terminate(patched), patched)

    def test_unknown_source_is_rejected(self):
        for source in ('', CALL + CALL + END, CALL):
            with self.subTest(source=source), self.assertRaises(ValueError):
                patch_terminate(source)

    def fixture(self):
        return {'metadata': {'name': 'atelet-version', 'resourceVersion': '42'},
                'spec': {'template': {'spec': {'containers': [
                    {'name': 'sidecar', 'image': 'leave-alone'},
                    {'name': 'atelet', 'image': 'old-image', 'securityContext': {
                        'runAsUser': 0, 'runAsGroup': 0,
                        'capabilities': {'drop': ['ALL']}}}]}}}}

    def test_patch_limits_privileges_to_atelet_and_preserves_existing_settings(self):
        original = self.fixture()
        before = copy.deepcopy(original)
        patch = daemonset_patch(original, 'localhost:5001/atelet@sha256:' + 'a' * 64)
        self.assertEqual(original, before)
        self.assertEqual(patch[0], {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '42'})
        security = patch[-1]['value']
        self.assertEqual(security['runAsUser'], 0)
        self.assertEqual(security['capabilities']['drop'], ['ALL'])
        self.assertEqual(security['capabilities']['add'], ['DAC_OVERRIDE', 'FOWNER'])
        self.assertNotIn('privileged', security)
        self.assertTrue(all('/containers/1/' in p['path'] for p in patch[1:]))

    def test_nonroot_or_ambiguous_atelet_is_rejected(self):
        original = self.fixture()
        original['spec']['template']['spec']['containers'][1]['securityContext']['runAsUser'] = 123
        with self.assertRaises(ValueError):
            daemonset_patch(original, 'image')

    def test_preview_does_not_execute_commands(self):
        with patch('sys.argv', ['substrate_cleanup.py']), \
             patch.object(substrate_cleanup.subprocess, 'run') as run:
            substrate_cleanup.main()
        run.assert_not_called()

    def test_wrong_kube_context_does_not_modify_source_or_cluster(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            script = root / 'ax_local/cluster/substrate_cleanup.py'
            script.parent.mkdir(parents=True)
            (root / 'ax_local/config.json').write_text(json.dumps({'ax': {'context': 'kind-test'}}))
            local = root / 'ax_local/.local'
            (local / 'bin').mkdir(parents=True)
            (local / 'bin/ko').touch()
            (local / 'kubeconfig').touch()
            with patch.object(substrate_cleanup, '__file__', str(script)), \
                 patch('sys.argv', [str(script), '--apply']), \
                 patch.object(substrate_cleanup.subprocess, 'run', return_value=
                              subprocess.CompletedProcess([], 0, 'company\n', '')) as run:
                with self.assertRaisesRegex(ValueError, 'current context'):
                    substrate_cleanup.main()
            run.assert_called_once()
            self.assertIn('--context=kind-test', run.call_args.args[0])
            self.assertEqual(run.call_args.kwargs['env']['KUBECONFIG'], str(local / 'kubeconfig'))

    def test_non_kind_configuration_prevents_all_commands(self):
        with patch.object(substrate_cleanup.json, 'loads', return_value={'ax': {'context': 'production'}}), \
             patch('sys.argv', ['substrate_cleanup.py', '--apply']), \
             patch.object(substrate_cleanup.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'local kind'):
                substrate_cleanup.main()
        run.assert_not_called()

    def test_build_failure_never_patches_cluster_and_success_uses_fresh_version(self):
        for fail_build in (True, False):
            with self.subTest(fail_build=fail_build), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                script = root / 'ax_local/cluster/substrate_cleanup.py'
                script.parent.mkdir(parents=True)
                shutil.copyfile(substrate_cleanup.__file__, script)
                shutil.copytree(Path(substrate_cleanup.__file__).with_name('cleanup_go'),
                                script.with_name('cleanup_go'))
                (root / 'ax_local/config.json').write_text(json.dumps({'ax': {'context': 'kind-test'}}))
                local = root / 'ax_local/.local'
                (local / 'bin').mkdir(parents=True)
                (local / 'bin/ko').touch()
                (local / 'kubeconfig').touch()
                source = local / 'src/substrate/cmd/atelet/main.go'
                source.parent.mkdir(parents=True)
                source.write_text(CALL + '\n\t}); err != nil {\n\t\treturn nil, err\n\t}\n' + END)
                calls = []
                live = self.fixture()

                def run(command, **kwargs):
                    calls.append(command)
                    if 'current-context' in command:
                        output = 'kind-test'
                    elif command[0] == 'git':
                        output = substrate_cleanup.PIN
                    elif 'daemonsets' in command:
                        output = json.dumps({'items': [live]})
                    elif command[0] == 'go':
                        output = 'arm64' if 'env' in command else ''
                    elif command[0].endswith('/ko'):
                        if fail_build:
                            raise subprocess.CalledProcessError(1, command)
                        output = 'localhost:5001/atelet@sha256:' + 'a' * 64
                    elif 'get' in command:
                        fresh = copy.deepcopy(live)
                        fresh['metadata']['resourceVersion'] = '43'
                        output = json.dumps(fresh)
                    elif 'patch' in command:
                        changes = json.loads(command[-1])
                        container = live['spec']['template']['spec']['containers'][1]
                        container['image'] = changes[1]['value']
                        container['securityContext'] = changes[2]['value']
                        output = ''
                    else:
                        output = ''
                    return subprocess.CompletedProcess(command, 0, output, '')

                with patch.object(substrate_cleanup, '__file__', str(script)), \
                     patch('sys.argv', [str(script), '--apply']), \
                     patch.object(substrate_cleanup.subprocess, 'run', side_effect=run):
                    if fail_build:
                        with self.assertRaises(subprocess.CalledProcessError):
                            substrate_cleanup.main()
                    else:
                        substrate_cleanup.main()
                writes = [c for c in calls if 'patch' in c]
                if fail_build:
                    self.assertEqual(writes, [])
                    self.assertFalse(any('rollout' in c for c in calls))
                else:
                    self.assertEqual(len(writes), 1)
                    self.assertEqual(json.loads(writes[0][-1])[0]['value'], '43')
                    self.assertIn('rollout', calls[-1])
                self.assertFalse(any('delete' in c for c in calls))
                if not fail_build:
                    with patch.object(substrate_cleanup, '__file__', str(script)), \
                         patch('sys.argv', [str(script), '--apply']), \
                         patch.object(substrate_cleanup.subprocess, 'run', side_effect=run):
                        calls.clear()
                        substrate_cleanup.main()
                        self.assertFalse(any(c[0].endswith('/ko') or 'patch' in c for c in calls))
                        self.assertIn('rollout', calls[-1])
                        for drift in ('image', 'capabilities'):
                            container = live['spec']['template']['spec']['containers'][1]
                            if drift == 'image':
                                container['image'] = 'upstream-image'
                            else:
                                container['securityContext']['capabilities']['add'] = []
                            calls.clear()
                            substrate_cleanup.main()
                            self.assertTrue(any('patch' in c for c in calls), drift)


if __name__ == '__main__':
    unittest.main()

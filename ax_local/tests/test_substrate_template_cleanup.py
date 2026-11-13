import unittest
from pathlib import Path
import json
import shutil
import subprocess
import tempfile
from unittest.mock import patch

from ax_local.cluster import substrate_api as cleanup


class TemplateCleanupPatchTests(unittest.TestCase):
    def test_patch_guards_template_deletion_and_is_idempotent(self):
        source = 'before\n' + cleanup.ANCHOR + '\nafter\n'
        updated = cleanup.patched_source(source)
        self.assertIn('s.popotPrepareTemplateDeletion(ctx, templateRef)', updated)
        self.assertIn('defer release()', updated)
        self.assertLess(updated.index('defer release()'), updated.index('s.impl.DeleteActorTemplate'))
        self.assertEqual(cleanup.patched_source(updated), updated)
        self.assertTrue(updated.endswith('after\n'))

    def test_unexpected_source_is_rejected(self):
        for source in ('unknown', cleanup.ANCHOR * 2,
                       cleanup.ANCHOR + 'popotPrepareTemplateDeletion'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                cleanup.patched_source(source)

    def test_non_kind_configuration_never_runs_commands(self):
        with patch.object(cleanup.json, 'loads', return_value={'context': 'production'}), \
                patch.object(cleanup.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'local kind'):
                cleanup.main()
        run.assert_not_called()

    def test_both_build_entrypoints_install_template_lifecycle_fix(self):
        root = Path(__file__).resolve().parents[2]
        for name in ('bootstrap.sh', 'rebuild-worker.sh'):
            with self.subTest(name=name):
                source = (root / 'ax_local' / name).read_text()
                self.assertIn('cluster/substrate_api.py', source)
                self.assertLess(source.index('cluster/substrate_api.py'), source.index('docker build'))

    def test_build_failure_stops_rollout_and_changed_pin_rebuilds_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'cluster/substrate_api.py'
            script.parent.mkdir()
            shutil.copyfile(cleanup.__file__, script)
            shutil.copytree(Path(cleanup.__file__).with_name('template_cleanup_go'),
                            script.with_name('template_cleanup_go'))
            local = root / '.local'
            source = local / 'src/substrate/cmd/ateapi/internal/controlapi/actor_template.go'
            source.parent.mkdir(parents=True)
            source.write_text(cleanup.ANCHOR)
            live = {'metadata': {'resourceVersion': '1'}, 'spec': {'template': {'spec': {
                'containers': [{'name': 'ate-api-server', 'image': 'old'}]}}}}
            calls = []
            fail = True

            def run(command, **kwargs):
                calls.append(command)
                output = ''
                if command[0] == 'git':
                    output = cleanup.PIN
                elif 'get' in command:
                    output = json.dumps(live)
                elif command[:2] == ['go', 'test'] and fail:
                    raise subprocess.CalledProcessError(1, command)
                elif command[:2] == ['go', 'env']:
                    output = 'arm64'
                elif command[0].endswith('/ko'):
                    output = 'localhost:5001/ateapi@sha256:' + 'a' * 64
                elif 'patch' in command:
                    live['spec']['template']['spec']['containers'][0]['image'] = json.loads(command[-1])[-1]['value']
                return subprocess.CompletedProcess(command, 0, output, '')

            with patch.object(cleanup, '__file__', str(script)), \
                    patch.object(cleanup.subprocess, 'run', side_effect=run):
                with self.assertRaises(subprocess.CalledProcessError):
                    cleanup.deploy(local, {'context': 'kind-test'})
                self.assertFalse(any('patch' in c for c in calls))
                self.assertFalse((local / 'substrate-api.json').exists())
                fail = False
                calls.clear()
                cleanup.deploy(local, {'context': 'kind-test'})
                self.assertTrue(any('patch' in c for c in calls))
                calls.clear()
                cleanup.deploy(local, {'context': 'kind-test'})
                self.assertFalse(any(c[0].endswith('/ko') for c in calls))
                calls.clear()
                with patch.object(cleanup, 'PIN', 'f' * 40):
                    cleanup.deploy(local, {'context': 'kind-test'})
                self.assertTrue(any(c[0].endswith('/ko') for c in calls), 'new Substrate commit must rebuild API')

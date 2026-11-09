"""Pinned runtime provisioning must verify storage before changing the cluster."""

import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = b'fixture release archive'


class GvisorRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='gvisor fixture ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        ax = self.root / 'ax_local'
        ax.mkdir()
        self.manifest = json.loads((ROOT / 'ax_local/gvisor-runtime.json').read_text())
        self.commit = self.manifest['metadata']['annotations']['popot.dev/gvisor-commit']
        self.manifest['metadata']['annotations']['popot.dev/gvisor-version'] = (
            'release-20261102.0-162-g93dbdc19004b')
        self.sha = hashlib.sha256(ARCHIVE).hexdigest()
        self.asset = self.manifest['spec']['assets']['arm64']['gvisor']
        self.asset['sha256'] = self.sha
        self.asset['url'] = f'gs://ate-snapshots/popot-runtime/gvisor/{self.commit}/arm64/{self.sha}/gvisor.tar.zstd'
        (ax / 'gvisor-runtime.json').write_text(json.dumps(self.manifest))
        (ax / 'config.json').write_text(json.dumps({'context': 'kind-popot-ax'}))
        self.cache = ax / '.local/gvisor' / self.commit
        self.cache.mkdir(parents=True)
        self.current = copy.deepcopy(self.manifest)
        self.current['metadata'] = {'name': 'gvisor-default', 'resourceVersion': '7',
                                    'annotations': {'owner': 'preserved'}}
        self.current['spec']['assets']['arm64']['gvisor'] = {
            'url': 'gs://gvisor/old.tar.zstd', 'sha256': 'a' * 64}
        self.current['spec']['assets']['amd64'] = {'runsc': {'sha256': 'b' * 64}}
        self.current['spec']['pauseImage'] = 'registry.example/pause@sha256:' + 'c' * 64
        self.remote = None
        self.calls = []
        self.fail_upload = False
        self.corrupt_download = False
        self.build_bytes = ARCHIVE
        self.deny_download = False

    def fake_run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if args[:2] == ['docker', 'rm']:
            return subprocess.CompletedProcess(args, 0, '', '')
        if args[0] == 'kubectl':
            self.assertIn('--context=kind-popot-ax', args)
            self.assertIn('--kubeconfig=' + str(self.root / 'ax_local/.local/kubeconfig'), args)
            if 'rollout' in args or 'wait' in args:
                return subprocess.CompletedProcess(args, 0, '', '')
            if 'patch' in args:
                patch = json.loads(args[-1])
                self.assertEqual(patch[0], {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '7'})
                for item in patch[1:]:
                    if item['path'] == '/spec/assets/arm64':
                        self.current['spec']['assets']['arm64'] = item['value']
                    elif item['path'] == '/metadata/annotations':
                        self.current['metadata']['annotations'] = item['value']
                return subprocess.CompletedProcess(args, 0, '', '')
            if 'sandboxconfig' in args:
                value = self.current
            elif 'deployment' in args:
                value = {'spec': {'template': {'spec': {'containers': [{'env': [
                    {'name': 'RUSTFS_ACCESS_KEY', 'value': 'fixture-access'},
                    {'name': 'RUSTFS_SECRET_KEY', 'valueFrom': {
                        'secretKeyRef': {'name': 'object-store', 'key': 'password'}}}]}]}}}}
            elif 'secret' in args:
                value = {'data': {'password': 'Zml4dHVyZS1zZWNyZXQ='}}
            else:
                value = {'spec': {'clusterIP': '10.96.1.2'}}
            return subprocess.CompletedProcess(args, 0, json.dumps(value), '')
        self.assertEqual(args[:2], ['docker', 'run'])
        if '/gvisor-build.sh' in args:
            self.assertNotIn('--privileged', args)
            self.assertNotIn('/var/run/docker.sock', ' '.join(args))
            self.assertIn(self.commit, args)
            (self.cache / 'gvisor.tar.zstd.partial').write_bytes(self.build_bytes)
            return subprocess.CompletedProcess(args, 0, '', '')
        self.assertIn('--network', args)
        self.assertIn('container:popot-ax-control-plane', args)
        self.assertNotIn('fixture-secret', ' '.join(args))
        self.assertEqual(kwargs['env']['AWS_SECRET_ACCESS_KEY'], 'fixture-secret')
        if 's3api' in args:
            return subprocess.CompletedProcess(args, 0, None, b'')
        cp = args.index('cp')
        if args[cp + 1] == '-':
            if self.fail_upload:
                raise subprocess.CalledProcessError(1, args, stderr=b'AccessDenied')
            self.remote = kwargs['stdin'].read()
        else:
            if self.deny_download:
                return subprocess.CompletedProcess(args, 1, None, b'AccessDenied')
            if self.remote is None:
                return subprocess.CompletedProcess(args, 1, None, b'NoSuchKey')
            kwargs['stdout'].write(b'corrupt' if self.corrupt_download else self.remote)
        return subprocess.CompletedProcess(args, 0, None, b'')

    def install(self, machine='arm64'):
        from ax_local.cluster.gvisor_runtime import ensure_runtime
        return ensure_runtime(self.root, run=self.fake_run, machine=machine)

    def test_clean_install_builds_stages_verifies_and_preserves_other_settings(self):
        result = self.install()
        self.assertTrue(result['changed'])
        self.assertEqual(self.remote, ARCHIVE)
        self.assertEqual(self.current['spec']['assets']['arm64']['gvisor'], self.asset)
        self.assertEqual(self.current['spec']['assets']['amd64'], {'runsc': {'sha256': 'b' * 64}})
        self.assertEqual(self.current['spec']['pauseImage'], 'registry.example/pause@sha256:' + 'c' * 64)
        self.assertEqual(self.current['metadata']['annotations']['owner'], 'preserved')
        patch = next(i for i, (args, _) in enumerate(self.calls) if 'patch' in args)
        downloads = [i for i, (args, _) in enumerate(self.calls) if 's3' in args and args[args.index('cp') + 2] == '-']
        self.assertLess(downloads[-1], patch)

    def test_repeat_uses_verified_cache_without_build_upload_or_patch(self):
        self.install()
        self.calls.clear()
        self.assertFalse(self.install()['changed'])
        self.assertFalse(any('patch' in args for args, _ in self.calls))
        docker = [args for args, _ in self.calls if args[:2] == ['docker', 'run']]
        self.assertEqual(len(docker), 2)
        copies = [args for args in docker if 'cp' in args]
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0][copies[0].index('cp') + 2], '-')

    def test_missing_local_cache_downloads_existing_verified_archive(self):
        self.remote = ARCHIVE
        self.install()
        self.assertEqual((self.cache / 'gvisor.tar.zstd').read_bytes(), ARCHIVE)
        self.assertFalse(any(args[:2] == ['docker', 'run'] and '/gvisor-build.sh' in args for args, _ in self.calls))

    def test_bad_local_archive_stops_before_external_calls(self):
        (self.cache / 'gvisor.tar.zstd').write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            self.install()
        self.assertEqual(self.calls, [])

    def test_corrupt_remote_archive_and_failed_upload_never_patch(self):
        for failure in ('download', 'upload', 'build'):
            with self.subTest(failure=failure):
                self.calls.clear()
                (self.cache / 'gvisor.tar.zstd').unlink(missing_ok=True)
                self.remote = ARCHIVE if failure == 'download' else None
                self.corrupt_download = failure == 'download'
                self.fail_upload = failure == 'upload'
                self.build_bytes = b'wrong build' if failure == 'build' else ARCHIVE
                with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                    self.install()
                self.assertFalse(any('patch' in args for args, _ in self.calls))

    def test_nonlocal_context_and_invalid_pin_are_rejected(self):
        (self.root / 'ax_local/config.json').write_text(json.dumps({'context': 'production'}))
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(self.calls, [])
        (self.root / 'ax_local/config.json').write_text(json.dumps({'context': 'kind-popot-ax'}))
        self.manifest['metadata']['annotations']['popot.dev/gvisor-commit'] = 'main'
        (self.root / 'ax_local/gvisor-runtime.json').write_text(json.dumps(self.manifest))
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(self.calls, [])

    def test_storage_access_failure_is_not_treated_as_a_missing_archive(self):
        self.deny_download = True
        with self.assertRaisesRegex(RuntimeError, 'download'):
            self.install()
        self.assertFalse(any('patch' in args for args, _ in self.calls))
        self.assertFalse(any(args[:2] == ['docker', 'run'] and '/gvisor-build.sh' in args for args, _ in self.calls))

    def test_timed_out_builder_is_removed_without_patching_runtime(self):
        from ax_local.cluster.gvisor_runtime import ensure_runtime
        original = self.fake_run

        def timeout_builder(args, **kwargs):
            if args[:2] == ['docker', 'run'] and '/gvisor-build.sh' in args:
                self.calls.append((args, kwargs))
                raise subprocess.TimeoutExpired(args, 3600)
            return original(args, **kwargs)

        with self.assertRaises(subprocess.TimeoutExpired):
            ensure_runtime(self.root, run=timeout_builder, machine='arm64')
        builder = next(args for args, _ in self.calls if args[:2] == ['docker', 'run'] and '/gvisor-build.sh' in args)
        cleanup = next(args for args, _ in self.calls if args[:2] == ['docker', 'rm'])
        self.assertEqual(cleanup[-1], builder[builder.index('--name') + 1])
        self.assertFalse(any('patch' in args for args, _ in self.calls))

    def test_storage_readiness_failure_prevents_build_and_runtime_change(self):
        from ax_local.cluster.gvisor_runtime import ensure_runtime
        original = self.fake_run

        def storage_not_ready(args, **kwargs):
            if 's3api' in args:
                raise subprocess.CalledProcessError(1, args)
            return original(args, **kwargs)

        with self.assertRaises(subprocess.CalledProcessError):
            ensure_runtime(self.root, run=storage_not_ready, machine='arm64')
        self.assertFalse(any('/gvisor-build.sh' in args or 'patch' in args for args, _ in self.calls))

    def test_concurrent_installers_build_and_publish_once(self):
        from ax_local.cluster.gvisor_runtime import ensure_runtime
        original = self.fake_run
        first_build = threading.Event()
        second_build = threading.Event()
        release = threading.Event()
        builds = []

        def delayed_build(args, **kwargs):
            if args[:2] == ['docker', 'run'] and '/gvisor-build.sh' in args:
                builds.append(args)
                (first_build if len(builds) == 1 else second_build).set()
                if not release.wait(5):
                    raise TimeoutError('test builder was not released')
            return original(args, **kwargs)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(ensure_runtime, self.root, run=delayed_build, machine='arm64')
            self.assertTrue(first_build.wait(3))
            second = pool.submit(ensure_runtime, self.root, run=delayed_build, machine='arm64')
            try:
                self.assertFalse(second_build.wait(.1))
            finally:
                release.set()
            self.assertTrue(first.result(timeout=3)['changed'])
            self.assertFalse(second.result(timeout=3)['changed'])
        self.assertEqual(len(builds), 1)

    def test_amd64_does_not_claim_the_arm64_fix(self):
        result = self.install('x86_64')
        self.assertFalse(result['installed'])
        self.assertEqual(self.calls, [])

    def test_entrypoints_install_before_starting_worker_checks(self):
        for name in ('bootstrap.sh', 'rebuild-worker.sh'):
            source = (ROOT / 'ax_local' / name).read_text()
            self.assertIn('cluster/gvisor_runtime.py', source)
            self.assertLess(source.index('cluster/gvisor_runtime.py'), source.index('cluster/worker_health.py'))
            if name == 'bootstrap.sh':
                self.assertLess(source.index('cluster/gvisor_runtime.py'), source.index('--deploy-demo-counter'))

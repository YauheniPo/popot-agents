"""Unit coverage for the pinned-runtime provisioning helpers."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ax_local.cluster import gvisor_runtime


ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = b'fixture release archive'


class RustfsEndpointTests(unittest.TestCase):
    def test_endpoint_scheme_is_configurable_and_validated(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('POPOT_RUSTFS_SCHEME', None)
            self.assertEqual(gvisor_runtime.rustfs_endpoint('10.96.1.2').split('://')[0], 'http')
        with patch.dict(os.environ, {'POPOT_RUSTFS_SCHEME': 'https'}):
            self.assertEqual(gvisor_runtime.rustfs_endpoint('10.96.1.2'),
                             'https://10.96.1.2:9000')
        with patch.dict(os.environ, {'POPOT_RUSTFS_SCHEME': 'ftp'}):
            with self.assertRaisesRegex(ValueError, 'POPOT_RUSTFS_SCHEME'):
                gvisor_runtime.rustfs_endpoint('10.96.1.2')


class GvisorPinTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='gvisor pin fixture ')
        self.addCleanup(self.temporary.cleanup)
        self.ax = Path(self.temporary.name).resolve() / 'ax_local'
        self.ax.mkdir()
        self.manifest = json.loads((ROOT / 'ax_local/gvisor-runtime.json').read_text())
        annotations = self.manifest['metadata']['annotations']
        annotations['popot.dev/gvisor-version'] = 'release-20261102.0-162-g93dbdc19004b'
        asset = self.manifest['spec']['assets']['arm64']['gvisor']
        commit = annotations['popot.dev/gvisor-commit']
        asset['sha256'] = 'a' * 64
        asset['url'] = f'gs://ate-snapshots/popot-runtime/gvisor/{commit}/arm64/{"a" * 64}/gvisor.tar.zstd'
        (self.ax / 'gvisor-runtime.json').write_text(json.dumps(self.manifest))

    def test_pin_with_foreign_archive_url_is_rejected(self):
        self.manifest['spec']['assets']['arm64']['gvisor']['url'] = 'gs://elsewhere/gvisor.tar.zstd'
        (self.ax / 'gvisor-runtime.json').write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(ValueError, 'content-addressed'):
            gvisor_runtime.load_pin(self.ax)

    def test_credentials_skip_unrelated_env_and_require_both_keys(self):
        env = [{'name': 'RUSTFS_LOG_LEVEL', 'value': 'info'},
               {'name': 'RUSTFS_ACCESS_KEY', 'value': 'access'},
               {'name': 'RUSTFS_SECRET_KEY', 'value': 'secret'}]

        def get(*args):
            return {'spec': {'template': {'spec': {'containers': [{'env': env}]}}}}

        self.assertEqual(gvisor_runtime.rustfs_credentials(get),
                         {'RUSTFS_ACCESS_KEY': 'access', 'RUSTFS_SECRET_KEY': 'secret'})
        env[:] = [{'name': 'RUSTFS_ACCESS_KEY', 'value': 'access'}]
        with self.assertRaisesRegex(ValueError, 'credentials are missing'):
            gvisor_runtime.rustfs_credentials(get)

    def test_uploaded_archive_must_be_readable_back(self):
        cache = self.ax / '.local/gvisor' / 'commit'
        cache.mkdir(parents=True)
        archive = cache / 'gvisor.tar.zstd'
        archive.write_bytes(ARCHIVE)
        sha = gvisor_runtime.hashlib.sha256(ARCHIVE).hexdigest()

        def run(args, **kwargs):
            return subprocess.CompletedProcess(args, 1, '', b'NoSuchKey')

        with self.assertRaisesRegex(RuntimeError, 'missing from RustFS'):
            gvisor_runtime.sync_archive(ROOT, cache, 'commit', 'version', sha,
                                        ['docker'], 's3://bucket/key', {}, run)

    def test_apply_runtime_rejects_foreign_and_stale_runtime(self):
        asset = {'url': 'gs://bucket/key', 'sha256': 'a' * 64}
        annotations = {'popot.dev/gvisor-commit': 'b' * 40}
        with self.assertRaisesRegex(ValueError, 'not gVisor'):
            gvisor_runtime.apply_runtime([], lambda: {'spec': {'sandboxClass': 'runc'}},
                                         asset, annotations, subprocess.run)
        stale = {'metadata': {'resourceVersion': '1'}, 'spec': {'sandboxClass': 'gvisor', 'assets': {}}}
        with self.assertRaisesRegex(RuntimeError, 'read-back'):
            gvisor_runtime.apply_runtime([], lambda: stale, asset, annotations,
                                         lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, '', ''))

    def test_module_entry_point_runs_the_installer(self):
        with patch.object(gvisor_runtime, 'ensure_runtime') as ensure:
            gvisor_runtime.main()
        ensure.assert_called_once()
        self.assertEqual(ensure.call_args.args[0], ROOT)

    def test_module_entry_point_skips_non_arm_hosts(self):
        import runpy
        with patch('platform.machine', return_value='x86_64'):
            namespace = runpy.run_path(str(ROOT / 'ax_local/cluster/gvisor_runtime.py'),
                                       run_name='__main__')
        self.assertIn('main', namespace)

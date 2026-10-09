"""The same checked-in pins must work in Docker's sh and local Python tooling."""

import subprocess
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


class BuildVersionTests(unittest.TestCase):
    def test_shell_and_python_read_the_same_shared_versions(self):
        from ax_local.cluster.build_versions import DEFAULT_PATH, load_build_versions
        versions = load_build_versions()
        result = subprocess.run(['sh', '-c', '. "$1"; printf "%s\\n" "$AX_VERSION" "$AX_COMMIT" "$SUBSTRATE_COMMIT" "$KO_VERSION"',
                                 'version-check', str(DEFAULT_PATH)],
                                check=True, capture_output=True, text=True)
        self.assertEqual(result.stdout.splitlines(),
                         [versions[key] for key in ('AX_VERSION', 'AX_COMMIT', 'SUBSTRATE_COMMIT', 'KO_VERSION')])

    def test_python_consumers_follow_changed_shared_commits(self):
        from ax_local.cluster.build_versions import DEFAULT_PATH
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / 'ax_local/cluster'
            scripts.mkdir(parents=True)
            (scripts.parent / '__init__.py').touch()
            (scripts / '__init__.py').touch()
            source = DEFAULT_PATH.parent.parent / 'cluster'
            for name in ('build_versions.py', 'controller_tools.py', 'worker_health.py',
                         'worker_pool.py', 'substrate_cleanup.py'):
                shutil.copyfile(source / name, scripts / name)
            (scripts.parent / 'docker').mkdir()
            path = scripts.parent / 'docker/versions.env'
            path.write_text('AX_VERSION=v0.4.0\nAX_COMMIT=' + 'a' * 40 + '\n'
                            'SUBSTRATE_COMMIT=' + 'b' * 40 + '\nKO_VERSION=v0.20.0\n')
            code = ('from ax_local.cluster import controller_tools, worker_health, substrate_cleanup; '
                    'assert controller_tools.AX_COMMIT == "a" * 40; '
                    'assert worker_health.SUBSTRATE_COMMIT == "b" * 40; '
                    'assert substrate_cleanup.PIN == "b" * 40')
            result = subprocess.run([sys.executable, '-c', code], cwd=root,
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_incomplete_duplicate_and_invalid_pins_are_rejected(self):
        from ax_local.cluster.build_versions import DEFAULT_PATH, load_build_versions
        original = DEFAULT_PATH.read_text()
        versions = load_build_versions()
        variants = [original.replace('AX_VERSION=' + versions['AX_VERSION'] + '\n', ''),
                    original + '\nAX_VERSION=v0.4.0\n',
                    original.replace('AX_VERSION=' + versions['AX_VERSION'], 'AX_VERSION=$(false)'),
                    original.replace(versions['AX_COMMIT'], 'short')]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'versions.env'
            for text in variants:
                with self.subTest(text=text):
                    path.write_text(text)
                    with self.assertRaises(ValueError):
                        load_build_versions(path)


if __name__ == '__main__':
    unittest.main()

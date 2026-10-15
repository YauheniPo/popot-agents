"""The same checked-in pins must work in Docker's sh and local Python tooling."""

import subprocess
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PINS = ('[project]\nname = "fixture"\n\n[tool.popot.build]\n'
        'AX_VERSION = "v0.4.0"\nAX_COMMIT = "' + 'a' * 40 + '"\n'
        'SUBSTRATE_COMMIT = "' + 'b' * 40 + '"\nKO_VERSION = "v0.20.0"\n')


class BuildVersionTests(unittest.TestCase):
    def test_shell_and_python_read_the_same_shared_versions(self):
        from ax_local.cluster.build_versions import DEFAULT_PATH, load_build_versions
        self.assertEqual(DEFAULT_PATH, ROOT / 'pyproject.toml')
        versions = load_build_versions()
        exported = subprocess.run([sys.executable, str(ROOT / 'ax_local/cluster/build_versions.py')],
                                  check=True, capture_output=True, text=True)
        result = subprocess.run(['sh', '-c', 'eval "$1"; printf "%s\\n" "$AX_VERSION" "$AX_COMMIT" "$SUBSTRATE_COMMIT" "$KO_VERSION"',
                                 'version-check', exported.stdout],
                                check=True, capture_output=True, text=True)
        self.assertEqual(result.stdout.splitlines(),
                         [versions[key] for key in ('AX_VERSION', 'AX_COMMIT', 'SUBSTRATE_COMMIT', 'KO_VERSION')])

    def test_python_consumers_follow_changed_shared_commits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / 'ax_local/cluster'
            scripts.mkdir(parents=True)
            (scripts.parent / '__init__.py').touch()
            (scripts / '__init__.py').touch()
            source = ROOT / 'ax_local/cluster'
            for name in ('build_versions.py', 'controller_tools.py', 'worker_health.py',
                         'worker_pool.py', 'substrate_cleanup.py'):
                shutil.copyfile(source / name, scripts / name)
            (root / 'pyproject.toml').write_text(PINS)
            code = ('from ax_local.cluster import controller_tools, worker_health, substrate_cleanup; '
                    'assert controller_tools.AX_COMMIT == "a" * 40; '
                    'assert worker_health.SUBSTRATE_COMMIT == "b" * 40; '
                    'assert substrate_cleanup.PIN == "b" * 40')
            result = subprocess.run([sys.executable, '-c', code], cwd=root,
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_incomplete_duplicate_and_invalid_pins_are_rejected(self):
        from ax_local.cluster.build_versions import load_build_versions
        variants = [PINS.replace('AX_VERSION = "v0.4.0"\n', ''),
                    PINS + '\nAX_VERSION = "v0.5.0"\n',
                    PINS.replace('v0.4.0', '$(false)'),
                    PINS.replace('a' * 40, 'short'),
                    PINS.replace('"v0.4.0"', '123'),
                    PINS + 'UNKNOWN = "v1.0.0"\n',
                    '[project]\nname = "fixture"\n']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'pyproject.toml'
            for text in variants:
                with self.subTest(text=text):
                    path.write_text(text)
                    with self.assertRaises(ValueError):
                        load_build_versions(path)

    def test_toml_comments_and_unrelated_sections_are_supported(self):
        from ax_local.cluster.build_versions import load_build_versions
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'pyproject.toml'
            path.write_text(PINS.replace('"v0.4.0"', "'v0.4.0' # release")
                            + '\n[tool.other]\nAX_VERSION = "ignored"\n')
            self.assertEqual(load_build_versions(path), {
                'AX_VERSION': 'v0.4.0', 'AX_COMMIT': 'a' * 40,
                'SUBSTRATE_COMMIT': 'b' * 40, 'KO_VERSION': 'v0.20.0'})


if __name__ == '__main__':
    unittest.main()

"""The same checked-in pins must work in Docker's sh and local Python tooling."""

import subprocess
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PINS = ('[project]\nname = "fixture"\n\n[tool.popot.build]\n'
        'AX_VERSION = "v0.4.0"\nAX_COMMIT = "' + 'a' * 40 + '"\n'
        'SUBSTRATE_COMMIT = "' + 'b' * 40 + '"\nKO_VERSION = "v0.20.0"\n')
SERVICE_PINS = {
    'OMNIROUTE_VERSION': '9.8.7', 'COGNEE_VERSION': 'main-ccccccc',
    'COGNEE_COMMIT': 'c' * 40, 'COGNEE_SOURCE_SHA256': 'd' * 64,
    'HOLMES_VERSION': '0.99.0', 'POSTGRES_VERSION': '18',
    'NGINX_VERSION': '1.29-alpine', 'KUBECTL_VERSION': 'v1.36.0',
    'PYTHON_IMAGE': 'python:3.13-slim',
    'GO_IMAGE': 'golang:1.28.0-bookworm',
    'NODE_IMAGE': 'node:24-alpine@sha256:' + 'e' * 64,
}
PINS += ''.join(f'{key} = "{value}"\n' for key, value in SERVICE_PINS.items())


class BuildVersionTests(unittest.TestCase):
    def test_service_versions_are_loaded_and_validated(self):
        from ax_local.cluster.build_versions import load_build_versions
        versions = load_build_versions()
        self.assertTrue(SERVICE_PINS.keys() <= versions.keys())

    def test_compose_receives_changed_pins_and_preserves_arguments_and_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cluster = root / 'ax_local/cluster'
            cluster.mkdir(parents=True)
            shutil.copyfile(ROOT / 'ax_local/cluster/build_versions.py', cluster / 'build_versions.py')
            shutil.copyfile(ROOT / 'compose.sh', root / 'compose.sh')
            (root / 'pyproject.toml').write_text(PINS)
            binary = root / 'bin'
            binary.mkdir()
            (binary / 'python3').symlink_to(sys.executable)
            docker = binary / 'docker'
            docker.write_text(f'#!{sys.executable}\nimport json,os,sys\n'
                              f'print(json.dumps({{"args":sys.argv[1:],"pins":{{k:os.environ.get(k) for k in {list(SERVICE_PINS)!r}}}}}))\n'
                              'raise SystemExit(int(os.environ.get("FAKE_DOCKER_EXIT", "0")))\n')
            docker.chmod(0o700)
            env = {**os.environ, 'PATH': str(binary) + os.pathsep + os.defpath,
                   'OMNIROUTE_VERSION': 'stale', 'FAKE_DOCKER_EXIT': '17'}
            result = subprocess.run(['bash', str(root / 'compose.sh'), '-f', 'path with spaces.yaml', 'config'],
                                    env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 17, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(output['args'], ['compose', '-f', 'path with spaces.yaml', 'config'])
            self.assertEqual(output['pins'], SERVICE_PINS)
            (root / 'pyproject.toml').write_text(PINS.replace('9.8.7', '$(false)'))
            invalid = subprocess.run(['bash', str(root / 'compose.sh'), 'config'],
                                     env=env, text=True, capture_output=True)
            self.assertNotEqual(invalid.returncode, 0)
            self.assertEqual(invalid.stdout, '')

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
                         'worker_pool.py', 'substrate_cleanup.py', 'holmes.py'):
                shutil.copyfile(source / name, scripts / name)
            (root / 'pyproject.toml').write_text(PINS)
            code = (f'import sys; sys.path.append({str(ROOT)!r}); '
                    'from ax_local.cluster import controller_tools, worker_health, substrate_cleanup, holmes; '
                    'assert controller_tools.AX_COMMIT == "a" * 40; '
                    'assert worker_health.SUBSTRATE_COMMIT == "b" * 40; '
                    'assert substrate_cleanup.PIN == "b" * 40; '
                    'assert holmes.IMAGE == "robustadev/holmes:0.99.0"; '
                    'assert holmes.investigation_job("Check AX", "test-model")["spec"]'
                    '["template"]["spec"]["containers"][0]["image"] == holmes.IMAGE')
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
                    PINS.replace('OMNIROUTE_VERSION = "9.8.7"\n', ''),
                    PINS.replace('python:3.13-slim', 'python:$(false)'),
                    PINS.replace('d' * 64, 'invalid-checksum'),
                    PINS.replace('main-ccccccc', 'main-aaaaaaa'),
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
                'SUBSTRATE_COMMIT': 'b' * 40, 'KO_VERSION': 'v0.20.0', **SERVICE_PINS})


if __name__ == '__main__':
    unittest.main()

"""Backend settings stay isolated while both runners share turn budgets."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ConfigSplitTests(unittest.TestCase):
    def run_python(self, code, **environment):
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT,
                                env={**os.environ, **environment}, text=True,
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_common_config_has_no_backend_sections(self):
        shared = json.loads((ROOT / 'config/runtime.json').read_text())
        self.assertNotIn('ax', shared)
        self.assertNotIn('docker', shared)
        ax = json.loads((ROOT / 'ax_local/config.json').read_text())
        docker = json.loads((ROOT / 'popot_agents/config.json').read_text())
        self.assertIn('context', ax)
        self.assertIn('max_concurrent_tasks', docker)
        self.assertTrue(shared.keys().isdisjoint(ax))
        self.assertIsInstance(docker["memory"], str)
        self.assertIsInstance(shared["memory"], dict)

    def test_shared_services_do_not_load_backend_configs(self):
        self.run_python('''
from popot_agents.worker.http_harness import run_http
from popot_agents.mcp_server import MAX_LOG_BODY_BYTES
from popot_agents.orchestrator.session_store import SessionStore
''', POPOT_AX_CONFIG='/missing/ax.json', POPOT_DOCKER_CONFIG='/missing/docker.json')

    def test_docker_launch_does_not_load_ax_config(self):
        self.run_python('''
from popot_agents.orchestrator.main import DockerAgentRunner
command = DockerAgentRunner()._docker_command('config-test', False)
assert '--memory' in command
''', POPOT_AX_CONFIG='/missing/ax.json')

    def test_docker_config_rejects_invalid_resources_and_unknown_fields(self):
        from popot_agents.orchestrator.docker_config import load_docker_config

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'docker.json'
            for field, value in [('pids_limit', 0), ('max_concurrent_tasks', True),
                                 ('memory', '1536Mi'), ('cpus', float('nan')),
                                 ('probe_timeout_seconds', float('inf')),
                                 ('startup_probe_interval_seconds', -1),
                                 ('ax', {})]:
                with self.subTest(field=field, value=value):
                    settings = load_docker_config()
                    settings[field] = value
                    path.write_text(json.dumps(settings))
                    with self.assertRaises(ValueError):
                        load_docker_config(path)
            settings = load_docker_config()
            del settings['memory']
            path.write_text(json.dumps(settings))
            with self.assertRaisesRegex(ValueError, 'fields'):
                load_docker_config(path)

    def test_common_config_rejects_old_combined_layout(self):
        from popot_agents.runtime_config import load_runtime_config

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'runtime.json'
            for backend in ('ax', 'docker'):
                settings = load_runtime_config()
                settings[backend] = {}
                path.write_text(json.dumps(settings))
                with self.assertRaisesRegex(ValueError, 'runtime config'):
                    load_runtime_config(path)

    def test_ax_admission_does_not_load_docker_config(self):
        self.run_python('''
import tempfile
from ax_local.api.ax_runner import AxAgentRunner
from popot_agents.orchestrator.main import create_server
runner = AxAgentRunner('worker@sha256:' + 'a' * 64, 'test', 'http://model/v1')
with tempfile.TemporaryDirectory() as directory:
    server = create_server({'test': runner}, port=0, session_dir=directory)
    try:
        assert server.task_slots is None
    finally:
        server.server_close()
runner.shutdown_worker_queue()
''', POPOT_DOCKER_CONFIG='/missing/docker.json')

    def test_three_custom_files_reach_their_runners(self):
        shared = json.loads((ROOT / 'config/runtime.json').read_text())
        docker = json.loads((ROOT / 'popot_agents/config.json').read_text())
        ax = json.loads((ROOT / 'ax_local/config.json').read_text())
        shared['timeouts']['turn_seconds'] = 240
        docker.update(memory='3g', cpus=1, max_concurrent_tasks=2)
        ax['task_resources']['limits']['memory'] = '4Gi'
        ax['max_workers'] = 4
        with tempfile.TemporaryDirectory() as directory:
            environment = {}
            for name, settings in [('RUNTIME', shared), ('DOCKER', docker), ('AX', ax)]:
                path = Path(directory) / f'{name}.json'
                path.write_text(json.dumps(settings))
                environment[f'POPOT_{name}_CONFIG'] = str(path)
            self.run_python('''
import tempfile
from popot_agents.orchestrator.main import DockerAgentRunner, create_server
from ax_local.api.ax_runner import AxAgentRunner
docker = DockerAgentRunner()
command = docker._docker_command('config-test', False)
assert command[command.index('--memory') + 1] == '3g'
assert command[command.index('--cpus') + 1] == '1'
assert 'HARNESS_TURN_TIMEOUT_SECONDS=225' in command
with tempfile.TemporaryDirectory() as directory:
    server = create_server({'test': docker}, port=0, session_dir=directory)
    try:
        assert server.task_slots.acquire(blocking=False)
        assert server.task_slots.acquire(blocking=False)
        assert not server.task_slots.acquire(blocking=False)
    finally:
        server.server_close()
runner = AxAgentRunner('worker@sha256:' + 'a' * 64, 'test', 'http://model/v1')
try:
    manifest = runner.task_manifest('0123456789abcdef')
    assert manifest['spec']['resources']['limits']['memory'] == '4Gi'
    assert runner.worker_queue.max_workers == 4
    env = {item['name']: item['value'] for item in manifest['spec']['env']}
    assert env['HARNESS_TURN_TIMEOUT_SECONDS'] == '225'
finally:
    runner.shutdown_worker_queue()
''', **environment)

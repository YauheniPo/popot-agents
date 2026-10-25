"""AX deployment settings stay separate from shared harness budgets."""

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
        self.assertIn('context', ax)
        self.assertTrue(shared.keys().isdisjoint(ax))
        self.assertIsInstance(shared["memory"], dict)

    def test_shared_services_do_not_load_backend_configs(self):
        self.run_python('''
from popot_agents.worker.http_harness import run_http
from popot_agents.mcp_server import MAX_LOG_BODY_BYTES
from popot_agents.orchestrator.session_store import SessionStore
''', POPOT_AX_CONFIG='/missing/ax.json')

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

    def test_ax_admission_has_no_second_task_limiter(self):
        self.run_python('''
import tempfile
from ax_local.api.ax_runner import AxAgentRunner
from popot_agents.orchestrator.main import create_server
runner = AxAgentRunner('worker@sha256:' + 'a' * 64, 'test', 'http://model/v1')
with tempfile.TemporaryDirectory() as directory:
    server = create_server({'test': runner}, port=0, session_dir=directory)
    try:
        assert not hasattr(server, 'task_slots')
    finally:
        server.server_close()
runner.shutdown_worker_queue()
''')

    def test_custom_runtime_and_ax_files_reach_the_runner(self):
        shared = json.loads((ROOT / 'config/runtime.json').read_text())
        ax = json.loads((ROOT / 'ax_local/config.json').read_text())
        shared['timeouts']['turn_seconds'] = 240
        ax['task_resources']['limits']['memory'] = '4Gi'
        ax['max_workers'] = 4
        with tempfile.TemporaryDirectory() as directory:
            environment = {}
            for name, settings in [('RUNTIME', shared), ('AX', ax)]:
                path = Path(directory) / f'{name}.json'
                path.write_text(json.dumps(settings))
                environment[f'POPOT_{name}_CONFIG'] = str(path)
            self.run_python('''
import tempfile
from ax_local.api.ax_runner import AxAgentRunner
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

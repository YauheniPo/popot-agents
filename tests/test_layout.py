import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class ProjectLayoutTests(unittest.TestCase):
    def test_profiles_are_configuration_without_a_standalone_runner(self):
        from popot_agents.orchestrator.main import load_agents

        profiles = load_agents(Path(__file__).resolve().parents[1] / 'config/agents.json')
        for name, profile in profiles.items():
            with self.subTest(profile=name):
                self.assertFalse(callable(profile))
                self.assertFalse(hasattr(profile, 'start_chat'))
                self.assertFalse(hasattr(profile, 'network'))
                self.assertFalse(hasattr(profile, 'image'))
                self.assertEqual(profile.session_mode, 'http')
                self.assertIn('HARNESS_BASE_URL', profile.environment)

    def test_standalone_docker_deployment_is_removed(self):
        root = Path(__file__).resolve().parents[1]
        for path in ('popot_agents/compose.yaml', 'popot_agents/config.json',
                     'popot_agents/docker/worker.Dockerfile',
                     'popot_agents/docker/orchestrator.Dockerfile',
                     'popot_agents/orchestrator/docker_config.py'):
            with self.subTest(path=path):
                self.assertFalse((root / path).exists())

    @unittest.skipUnless(shutil.which('docker'), 'Docker Compose is required')
    def test_ax_compose_uses_shared_paths_without_docker_socket(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / '.env'
            env_file.write_text('POPOT_DB_PASSWORD=layout-test\n')
            result = subprocess.run(
                ['docker', 'compose', '--env-file', str(env_file), '-f',
                 str(root / 'ax_local/compose.yaml'), 'config',
                 '--format', 'json', '--no-env-resolution'], cwd=directory,
                env={key: value for key, value in os.environ.items()
                     if not key.startswith('COMPOSE_')},
                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(result.stdout)
        self.assertEqual(config['name'], 'popot-ax-local')
        services = config['services']
        self.assertEqual(set(services), {'db', 'api', 'mcp-server'})
        for name, dockerfile in {
            'api': 'ax_local/docker/api.Dockerfile',
            'mcp-server': 'docker/mcp.Dockerfile',
        }.items():
            with self.subTest(service=name):
                self.assertEqual(services[name]['build']['context'], str(root))
                self.assertEqual(services[name]['build']['dockerfile'], dockerfile)
                self.assertTrue((root / dockerfile).is_file())
        self.assertEqual(services['api']['env_file'][0]['path'], str(root / '.env'))
        for service in services.values():
            for mount in service.get('volumes', []):
                self.assertNotIn('docker.sock', mount.get('source', ''))
                self.assertNotIn('docker.sock', mount['target'])

    def test_orchestrator_loads_profiles_and_roles_from_config_directory(self):
        from popot_agents.orchestrator.main import load_agents, load_roles

        config = Path(__file__).resolve().parents[1] / "config"
        agents = load_agents(config / "agents.json")
        roles = load_roles(config / "roles.json", agents)
        self.assertIn("nous", agents)
        self.assertIn("backend_engineer", roles)

    def test_worker_entrypoints_are_importable(self):
        from popot_agents.worker.agent_worker import run_harness
        from popot_agents.worker.http_harness import run_http
        from popot_agents.worker.session_worker import Conversation

        self.assertTrue(callable(run_harness))
        self.assertTrue(callable(run_http))
        self.assertTrue(callable(Conversation))


if __name__ == "__main__":
    unittest.main()

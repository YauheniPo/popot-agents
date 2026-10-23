import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class ProjectLayoutTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('docker'), 'Docker Compose is required')
    def test_docker_compose_keeps_shared_paths_and_existing_project_identity(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory).resolve() / 'project'
            (project_root / 'popot_agents').mkdir(parents=True)
            compose_file = project_root / 'popot_agents/compose.yaml'
            shutil.copyfile(root / 'popot_agents/compose.yaml', compose_file)
            env_file = project_root / '.env'
            env_file.write_text('POPOT_DB_PASSWORD=layout-test\n')
            result = subprocess.run(
                ['docker', 'compose', '--env-file', str(env_file), '-f',
                 str(compose_file), 'config',
                 '--format', 'json'], cwd=directory,
                env={key: value for key, value in os.environ.items()
                     if not key.startswith('COMPOSE_')},
                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(result.stdout)
        self.assertEqual(config['name'], 'popot-agents')
        services = config['services']
        for name, dockerfile in {
            'worker-image': 'popot_agents/docker/worker.Dockerfile',
            'orchestrator': 'popot_agents/docker/orchestrator.Dockerfile',
            'mcp-server': 'docker/mcp.Dockerfile',
        }.items():
            with self.subTest(service=name):
                self.assertEqual(services[name]['build']['context'], str(project_root))
                self.assertEqual(services[name]['build']['dockerfile'], dockerfile)
                self.assertTrue((root / dockerfile).is_file())
                self.assertEqual(services[name]['develop']['watch'][0]['path'], str(project_root))
        orchestrator = services['orchestrator']
        self.assertEqual(orchestrator['environment']['POPOT_DB_PASSWORD'], 'layout-test')
        mounts = {item['target']: item['source'] for item in orchestrator['volumes']}
        self.assertEqual(mounts['/data/legacy-sessions'], str(project_root / '.sessions'))
        self.assertFalse((root / 'compose.yaml').exists())

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

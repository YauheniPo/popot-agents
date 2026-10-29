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
            project_root = Path(directory).resolve() / 'project'
            (project_root / 'ax_local').mkdir(parents=True)
            compose_file = project_root / 'ax_local/compose.yaml'
            shutil.copyfile(root / 'ax_local/compose.yaml', compose_file)
            env_file = project_root / '.env'
            env_file.write_text('POPOT_DB_PASSWORD=layout-test\n')
            result = subprocess.run(
                ['bash', str(root / 'compose.sh'), '--env-file', str(env_file), '-f',
                 str(compose_file), 'config',
                 '--format', 'json'], cwd=directory,
                env={key: value for key, value in os.environ.items()
                     if not key.startswith('COMPOSE_') and key != 'POPOT_DB_PASSWORD'},
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
                self.assertEqual(services[name]['build']['context'], str(project_root))
                self.assertEqual(services[name]['build']['dockerfile'], dockerfile)
                self.assertTrue((root / dockerfile).is_file())
        from ax_local.cluster.build_versions import BUILD_VERSIONS
        self.assertEqual(services['db']['image'], 'postgres:' + BUILD_VERSIONS['POSTGRES_VERSION'])
        self.assertEqual(services['db'].get('restart'), 'unless-stopped')
        self.assertEqual(services['api']['build']['additional_contexts'], {
            'python-base': 'docker-image://' + BUILD_VERSIONS['PYTHON_IMAGE'],
            'go-base': 'docker-image://' + BUILD_VERSIONS['GO_IMAGE']})
        self.assertEqual(services['mcp-server']['build']['additional_contexts'], {
            'python-base': 'docker-image://' + BUILD_VERSIONS['PYTHON_IMAGE']})
        self.assertEqual(services['api']['environment']['POPOT_DB_PASSWORD'], 'layout-test')
        for service in services.values():
            for mount in service.get('volumes', []):
                self.assertNotIn('docker.sock', mount.get('source', ''))
                self.assertNotIn('docker.sock', mount['target'])

    @unittest.skipUnless(shutil.which('docker'), 'Docker Compose is required')
    def test_memory_and_omniroute_compose_use_shared_pins(self):
        from ax_local.cluster.build_versions import BUILD_VERSIONS
        root = Path(__file__).resolve().parents[1]
        pins = {**BUILD_VERSIONS, 'OMNIROUTE_VERSION': '9.8.7',
                'COGNEE_VERSION': 'main-ccccccc', 'COGNEE_COMMIT': 'c' * 40,
                'COGNEE_SOURCE_SHA256': 'd' * 64, 'POSTGRES_VERSION': '18',
                'NGINX_VERSION': '1.29-alpine', 'NODE_IMAGE': 'node:24-alpine',
                'PYTHON_IMAGE': 'python:3.13-slim'}
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / 'project'
            (fixture / 'ax_local/cluster').mkdir(parents=True)
            (fixture / 'memory/ui').mkdir(parents=True)
            for path in ('compose.sh', 'ax_local/cluster/build_versions.py',
                         'ax_local/compose.yaml', 'memory/compose.yaml'):
                shutil.copyfile(root / path, fixture / path)
            (fixture / 'pyproject.toml').write_text('[tool.popot.build]\n' + ''.join(
                f'{key} = "{value}"\n' for key, value in pins.items()))
            env_file = fixture / '.env'
            env_file.write_text('COGNEE_PASSWORD=layout-test\nPOPOT_DB_PASSWORD=layout-test\n')
            configs = []
            for path in ('memory/compose.yaml', 'ax_local/compose.yaml'):
                result = subprocess.run(
                    ['bash', str(fixture / 'compose.sh'), '--env-file', str(env_file),
                     '-f', str(fixture / path), '--profile', 'omniroute', 'config',
                     '--format', 'json'],
                    env={key: value for key, value in os.environ.items()
                         if not key.startswith('COMPOSE_')}, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                configs.append(json.loads(result.stdout)['services'])
        memory, ax = configs
        self.assertEqual(ax['api']['build']['additional_contexts']['python-base'],
                         'docker-image://' + pins['PYTHON_IMAGE'])
        self.assertEqual(ax['mcp-server']['build']['additional_contexts'], {
            'python-base': 'docker-image://' + pins['PYTHON_IMAGE']})
        self.assertEqual(ax['omniroute']['image'],
                         'diegosouzapw/omniroute:' + pins['OMNIROUTE_VERSION'])
        self.assertEqual(ax['db']['image'], 'postgres:' + pins['POSTGRES_VERSION'])
        self.assertEqual(memory['cognee']['image'], 'cognee/cognee:' + pins['COGNEE_VERSION'])
        self.assertEqual(memory['cognee-web']['image'], 'nginx:' + pins['NGINX_VERSION'])
        self.assertEqual(memory['cognee-ui']['image'],
                         'popot-cognee-ui:' + pins['COGNEE_VERSION'] + '-knowledge')
        self.assertEqual(memory['cognee-ui']['build']['args'], {
            key: pins[key] for key in
            ('COGNEE_COMMIT', 'COGNEE_SOURCE_SHA256')})
        self.assertEqual(memory['cognee-ui']['build']['additional_contexts'], {
            'node-base': 'docker-image://' + pins['NODE_IMAGE'],
            'cognee-ui-base': 'docker-image://cognee/cognee-ui:' + pins['COGNEE_VERSION']})

    def test_orchestrator_loads_profiles_and_roles_from_config_directory(self):
        from popot_agents.orchestrator.main import load_agents, load_roles

        config = Path(__file__).resolve().parents[1] / "config"
        agents = load_agents(config / "agents.json")
        roles = load_roles(config / "roles", agents)
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

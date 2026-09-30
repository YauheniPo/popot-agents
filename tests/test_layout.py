import unittest
from pathlib import Path


class ProjectLayoutTests(unittest.TestCase):
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

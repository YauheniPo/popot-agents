import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from popot_agents.orchestrator.main import DockerAgentRunner


class OrchestratorContainerTests(unittest.TestCase):
    def test_worker_mount_uses_daemon_host_path_and_creates_local_workspace(self):
        role = {"permissions": {"workspace": "persistent"}}
        with tempfile.TemporaryDirectory() as directory:
            local_root = Path(directory) / "inside"
            host_root = Path(directory) / "outside"
            with patch.dict(os.environ, {
                "AGENT_WORKSPACE_DIR": str(local_root),
                "AGENT_WORKSPACE_HOST_DIR": str(host_root),
            }):
                runner = DockerAgentRunner()
                command = runner._docker_command("popot-chat-test", True, role)
                self.assertIn(
                    f"type=bind,src={host_root / 'popot-chat-test'},dst=/workspace",
                    command,
                )
                self.assertTrue((local_root / "popot-chat-test").is_dir())
                self.assertEqual(
                    runner.workspace_host_path("popot-chat-test", role),
                    host_root / "popot-chat-test",
                )


if __name__ == "__main__":
    unittest.main()

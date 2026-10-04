import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from popot_agents.orchestrator.main import load_roles
from popot_agents.worker import agent_worker, session_worker
from popot_agents.worker.http_harness import run_http


class SkillTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.skill = self.root / "engineering" / "testing"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text(
            "---\nname: testing\ndescription: Test behavior\n---\nUnique skill instruction.\n")
        (self.skill / "references.md").write_text("Unique reference example.\n")
        other = self.root / "other"
        other.mkdir()
        (other / "SKILL.md").write_text("Unselected private skill.\n")
        self.role = {"agent": "test", "instructions": "Base role instruction.",
                     "tools": [], "skills": ["engineering/testing"]}
        self.env = {"HARNESS_BASE_URL": "https://model.example/v1", "HARNESS_MODEL": "test",
                    "HARNESS_SESSION_MODE": "http", "HARNESS_ROLE_JSON": json.dumps(self.role)}
        self.root_patch = patch("popot_agents.skills.SKILLS_ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def model_response(self):
        return io.BytesIO(b'{"choices":[{"message":{"content":"done"}}]}')

    def assert_instructions(self, content):
        self.assertIn("Base role instruction.", content)
        self.assertIn("Unique skill instruction.", content)
        self.assertIn("Unique reference example.", content)
        self.assertNotIn("Unselected private skill.", content)
        self.assertEqual(content.count("Unique skill instruction."), 1)

    def test_role_selection_is_validated_without_expanding_stored_config(self):
        path = self.root / "roles.json"
        path.write_text(json.dumps({"engineer": self.role}))
        loaded = load_roles(path, {"test": SimpleNamespace(session_mode="http")})
        self.assertEqual(loaded["engineer"], self.role)
        for selection in (None, "testing", [1], ["missing"], ["../other"],
                          [str(self.skill)], ["engineering//testing"],
                          ["engineering/testing", "engineering/testing"]):
            with self.subTest(selection=selection):
                path.write_text(json.dumps({"engineer": dict(self.role, skills=selection)}))
                with self.assertRaisesRegex(ValueError, "skills"):
                    load_roles(path, {"test": SimpleNamespace(session_mode="http")})

    def test_http_model_receives_only_selected_documents(self):
        with patch.dict(os.environ, self.env), \
                patch("popot_agents.worker.http_harness.request.urlopen",
                      return_value=self.model_response()) as request:
            self.assertEqual(run_http("hello", self.role), "done")
        body = json.loads(request.call_args.args[0].data)
        self.assert_instructions(body["messages"][0]["content"])
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(body["messages"][1], {"role": "user", "content": "hello"})
        self.assertEqual(self.role["instructions"], "Base role instruction.")

    def test_docker_and_ax_forward_selection_to_worker_startup(self):
        from ax_local.api.ax_runner import AxAgentRunner
        from popot_agents.orchestrator.main import DockerAgentRunner
        from popot_agents.skills import prepare_role_config

        docker = DockerAgentRunner()
        command = docker._docker_command("popot-chat-test", True, self.role)
        docker_json = next(value.split("=", 1)[1] for value in command
                           if value.startswith("HARNESS_ROLE_JSON="))
        ax = AxAgentRunner(image="localhost:5001/popot-agent-ax@sha256:" + "a" * 64,
                           model="test", base_url="http://localhost:11434/v1",
                           context="kind-popot-ax")
        manifest = ax.task_manifest("0123456789abcdef", self.role)
        ax_json = next(item["value"] for item in manifest["spec"]["env"]
                       if item["name"] == "HARNESS_ROLE_JSON")
        for value in (docker_json, ax_json):
            self.assertEqual(json.loads(value), self.role)
            self.assert_instructions(prepare_role_config(json.loads(value))["instructions"])

    def test_session_snapshots_skills_at_start_for_http_and_cli(self):
        for mode in ("http", "cli"):
            with self.subTest(mode=mode):
                (self.skill / "references.md").write_text("Unique reference example.\n")
                with patch.dict(os.environ, dict(self.env, HARNESS_SESSION_MODE=mode)), \
                        patch("popot_agents.worker.session_worker.socketserver.UnixStreamServer") as server, \
                        patch("popot_agents.worker.http_harness.request.urlopen",
                              side_effect=lambda *a, **kw: self.model_response()) as request, \
                        patch("popot_agents.worker.session_worker.run_harness", return_value="done") as cli:
                    session_worker.serve()
                    (self.skill / "references.md").write_text("Changed after startup.")
                    handler_class = server.call_args.args[1]
                    for _ in range(2):
                        handler = handler_class.__new__(handler_class)
                        handler.rfile = io.BytesIO(b'{"action":"message","message":"hello"}\n')
                        handler.wfile = io.BytesIO()
                        handler.handle()
                        self.assertEqual(json.loads(handler.wfile.getvalue()), {"answer": "done"})
                    content = (json.loads(request.call_args.args[0].data)["messages"][0]["content"]
                               if mode == "http" else cli.call_args.args[0])
                    self.assert_instructions(content)
                    self.assertNotIn("Changed after startup.", content)

    def test_one_shot_cli_receives_skills(self):
        with patch.dict(os.environ, dict(self.env, HARNESS_SESSION_MODE="cli")), \
                patch("sys.stdin", io.StringIO('{"task":"hello"}')), \
                patch("popot_agents.worker.agent_worker.run_harness", return_value="done") as cli, \
                redirect_stdout(io.StringIO()):
            agent_worker.main()
        self.assert_instructions(cli.call_args.args[0])

    def test_empty_selection_preserves_instructions_and_emits_no_skill_log(self):
        from popot_agents.skills import prepare_role_config
        for config in ({}, {"instructions": "Original"}, {"instructions": "Original", "skills": []}):
            with redirect_stderr(io.StringIO()) as logs:
                result = prepare_role_config(config)
            self.assertEqual(result.get("instructions"), config.get("instructions"))
            self.assertEqual(logs.getvalue(), "")

    def test_rejects_symlinks_missing_entrypoint_and_oversized_content(self):
        from popot_agents.skills import prepare_role_config
        from popot_agents.runtime_config import RUNTIME
        (self.root / "linked").symlink_to(self.skill, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "skills"):
            prepare_role_config(dict(self.role, skills=["linked"]))
        (self.skill / "link.md").symlink_to(self.root / "other" / "SKILL.md")
        with self.assertRaisesRegex(ValueError, "skills"):
            prepare_role_config(self.role)
        (self.skill / "link.md").unlink()
        with patch.dict(RUNTIME["limits"], {"skill_prompt_bytes": 32}):
            with self.assertRaisesRegex(ValueError, "skills"):
                prepare_role_config(self.role)
        (self.skill / "SKILL.md").unlink()
        with self.assertRaisesRegex(ValueError, "skills"):
            prepare_role_config(self.role)


if __name__ == "__main__":
    unittest.main()

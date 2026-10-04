import io
import json
import os
import subprocess
import unittest
from unittest.mock import patch

from ax_local.config import AX_CONFIG
from ax_local.api.ax_runner import AxAgentRunner, AxChat
from popot_agents.orchestrator.main import AgentRunError
from popot_agents.runtime_config import RUNTIME


class AxRunnerTests(unittest.TestCase):
    def test_task_adds_only_selected_role_env_values(self):
        with patch.dict(RUNTIME, {"role_env_names": ["GITHUB_TOKEN"]}), \
             patch.dict(os.environ, {"GITHUB_TOKEN": "selected-secret", "OTHER_TOKEN": "other-secret"}):
            manifest = self.runner.task_manifest("0123456789abcdef", {
                "instructions": "x", "tools": [], "env_names": ["GITHUB_TOKEN"]})
        env = {item["name"]: item["value"] for item in manifest["spec"]["env"]}
        self.assertEqual(env["GITHUB_TOKEN"], "selected-secret")
        self.assertNotIn("OTHER_TOKEN", env)
        self.assertNotIn("selected-secret", env["HARNESS_ROLE_JSON"])
        self.assertEqual(json.loads(env["HARNESS_TOOL_ENV_NAMES_JSON"]), ["GITHUB_TOKEN"])
        with patch.dict(RUNTIME, {"role_env_names": []}), \
             patch.dict(os.environ, {"GITHUB_TOKEN": "selected-secret"}):
            revoked = self.runner.task_manifest("0123456789abcdef", {
                "env_names": ["GITHUB_TOKEN"]})
        self.assertNotIn("GITHUB_TOKEN", {item["name"] for item in revoked["spec"]["env"]})
        revoked_env = {item["name"]: item["value"] for item in revoked["spec"]["env"]}
        self.assertEqual(json.loads(revoked_env["HARNESS_TOOL_ENV_NAMES_JSON"]), [])

    def test_task_rejects_missing_role_env_without_exposing_values(self):
        with patch.dict(RUNTIME, {"role_env_names": ["GITHUB_TOKEN"]}), \
             patch.dict(os.environ, {"GITHUB_TOKEN": ""}):
            with self.assertRaisesRegex(Exception, "GITHUB_TOKEN"):
                self.runner.task_manifest("0123456789abcdef", {"env_names": ["GITHUB_TOKEN"]})

    def setUp(self):
        self.runner = AxAgentRunner(
            image="localhost:5001/popot-agent-ax@sha256:" + "a" * 64,
            model="test-model",
            base_url="http://host.docker.internal:11434/v1",
            context="kind-popot-ax",
        )

    def test_task_uses_existing_harness_without_credential_values(self):
        manifest = self.runner.task_manifest("0123456789abcdef", {
            "instructions": "Be concise", "tools": ["calculate"],
        })
        self.assertEqual(manifest["kind"], "Task")
        self.assertEqual(manifest["metadata"]["name"], "popot-chat-0123456789abcdef")
        self.assertEqual(manifest["spec"]["command"],
                         ["python", "-u", "-m", "ax_local.worker.entrypoint"])
        self.assertTrue(manifest["spec"]["debug"])
        env = {item["name"]: item["value"] for item in manifest["spec"]["env"]}
        self.assertEqual(env["HARNESS_MODEL"], "test-model")
        self.assertEqual(env["HARNESS_SESSION_MODE"], "http")
        self.assertEqual(json.loads(env["HARNESS_ROLE_JSON"])["tools"], ["calculate"])
        self.assertNotIn("OPENROUTER_API_KEY", json.dumps(manifest))

    def test_only_tool_roles_request_privilege_drop_capabilities(self):
        for config in ({"tools": ["bash"]}, {"tools": ["write_file"]},
                       {"mcpServers": {"fetch": {}}}, {"tools": ["calculate"]}, {}):
            with self.subTest(config=config):
                manifest = self.runner.task_manifest("0123456789abcdef", config)
                env = {item["name"]: item["value"] for item in manifest["spec"]["env"]}
                if config.get("mcpServers") or set(config.get("tools", [])) & {"bash", "write_file"}:
                    self.assertEqual(env["POPOT_TOOLS_UID"], "10001")
                else:
                    self.assertNotIn("POPOT_TOOLS_UID", env)

    def test_task_uses_proxy_capability_not_provider_key(self):
        runner = AxAgentRunner(
            image=self.runner.image, model="example/model",
            base_url="http://172.18.0.5:8004/v1", context="kind-popot-ax",
            proxy_token="session-capability",
        )
        manifest = runner.task_manifest("0123456789abcdef")
        env = {item["name"]: item["value"] for item in manifest["spec"]["env"]}
        self.assertEqual(env["HARNESS_API_KEY_ENV"], "AX_PROXY_TOKEN")
        self.assertEqual(env["AX_PROXY_TOKEN"], "session-capability")
        self.assertNotIn("private-provider-key", json.dumps(manifest))

    def test_ax_manifest_uses_configured_workspace_and_resources(self):
        ax = AX_CONFIG["ax"]
        self.assertEqual(self.runner.workspace_manifest()["metadata"]["name"],
                         ax["workspace_name"])
        manifest = self.runner.task_manifest("0123456789abcdef")
        self.assertEqual(manifest["metadata"]["atespace"], ax["atespace"])
        self.assertEqual(manifest["spec"]["resources"], ax["task_resources"])
        self.assertEqual(manifest["spec"]["debug"], ax["task_debug"])

    def test_requires_local_context_and_digest_image(self):
        with self.assertRaises(ValueError):
            AxAgentRunner(image="worker:latest", model="x", base_url="http://localhost:1/v1")
        with self.assertRaises(ValueError):
            AxAgentRunner(image=self.runner.image, model="x", base_url="http://localhost:1/v1",
                          context="production")

    def test_chat_send_and_restore_use_ax_ssh_without_stdin(self):
        chat = AxChat(self.runner, "popot-chat-0123456789abcdef", 12)
        with patch.object(self.runner, "remote", side_effect=[
            {"status": "ok"}, {"answer": "hello"},
        ]) as remote:
            chat.restore([{"role": "user", "content": "hi"},
                          {"role": "assistant", "content": "hello"}])
            self.assertEqual(chat.send("again"), {"answer": "hello"})
        self.assertEqual(remote.call_args_list[0].args[1]["action"], "restore")
        self.assertEqual(remote.call_args_list[1].args[1]["message"], "again")

    def test_probe_reports_safe_ssh_failure_category(self):
        chat = AxChat(self.runner, "popot-chat-0123456789abcdef", 12)
        with patch.object(self.runner, "remote",
                          side_effect=AgentRunError("private-provider-key: connection refused")):
            self.assertFalse(chat.is_alive())
        self.assertEqual(chat.probe_issue, "AX ssh connection failed")

    def test_start_creates_workspace_and_task_then_probes(self):
        missing = subprocess.CompletedProcess([], 1, "", "rpc error: code = NotFound")
        applied = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(self.runner, "_run", side_effect=[missing, applied, applied]) as run, \
             patch.object(self.runner, "remote", return_value={"status": "ok"}):
            chat = self.runner.start_chat("0123456789abcdef")
        self.assertEqual(chat.container_name, "popot-chat-0123456789abcdef")
        manifests = [json.loads(call.kwargs["input"]) for call in run.call_args_list
                     if "input" in call.kwargs]
        self.assertEqual([item["kind"] for item in manifests], ["Workspace", "Task"])

    def test_lookup_connection_error_does_not_apply_task(self):
        unavailable = subprocess.CompletedProcess([], 1, "", "connection refused")
        with patch.object(self.runner, "_run", return_value=unavailable) as run:
            with self.assertRaises(AgentRunError):
                self.runner.start_chat("0123456789abcdef")
        run.assert_called_once()

    def test_expired_parent_deadline_prevents_child_start(self):
        with patch.object(self.runner, "_run") as run, \
             patch("ax_local.api.ax_runner.time.monotonic", return_value=20):
            with self.assertRaisesRegex(TimeoutError, "deadline"):
                self.runner.start_chat("0123456789abcdef", deadline=10)
        run.assert_not_called()

    def test_ax_manifest_gives_harness_the_role_turn_budget(self):
        manifest = self.runner.task_manifest("0123456789abcdef", {"timeout_seconds": 300})
        env = {item["name"]: item["value"] for item in manifest["spec"]["env"]}
        self.assertEqual(env["HARNESS_TURN_TIMEOUT_SECONDS"], "300")

    def test_existing_task_is_replaced_after_api_restart(self):
        present = subprocess.CompletedProcess([], 0, "", "")
        deleted = subprocess.CompletedProcess([], 0, "", "")
        applied = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(self.runner, "_run", side_effect=[present, deleted, applied, applied]) as run, \
             patch.object(self.runner, "remote", return_value={"status": "ok"}):
            self.runner.start_chat("0123456789abcdef")
        self.assertEqual(run.call_args_list[1].args[:3],
                         ("delete", "task", "popot-chat-0123456789abcdef"))

    def test_cleanup_timeout_identifies_session_start_stage_without_applying(self):
        present = subprocess.CompletedProcess([], 0, "", "")
        error = subprocess.TimeoutExpired(["ax", "private-command-value"], 17,
                                          stderr="private-stderr-value")
        with patch.dict(AX_CONFIG["ax"], {"cli_timeout_seconds": 17}), \
             patch.object(self.runner, "_run", side_effect=[present, error]) as run:
            with self.assertRaisesRegex(AgentRunError, "deleting previous AX task") as caught:
                self.runner.start_chat("0123456789abcdef")
        self.assertIn("popot-chat-0123456789abcdef", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.kwargs["timeout"], 17)

    def test_cleanup_uses_remaining_parent_budget(self):
        present = subprocess.CompletedProcess([], 0, "", "")
        error = subprocess.TimeoutExpired(["ax"], 2)
        with patch.dict(AX_CONFIG["ax"], {"cli_timeout_seconds": 17}), \
             patch.object(self.runner, "_run", side_effect=[present, error]) as run, \
             patch("ax_local.api.ax_runner.time.monotonic", return_value=8):
            with self.assertRaises(AgentRunError):
                self.runner.start_chat("0123456789abcdef", deadline=10)
        self.assertEqual(run.call_args.kwargs["timeout"], 2)

    def test_close_reports_unconfirmed_cleanup_without_exposing_stderr(self):
        failures = [subprocess.CompletedProcess([], 1, "", "private-stderr-value"),
                    subprocess.TimeoutExpired(["ax", "private-command-value"], 5)]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                chat = AxChat(self.runner, "popot-chat-0123456789abcdef", 12)
                kwargs = ({"side_effect": failure} if isinstance(failure, Exception)
                          else {"return_value": failure})
                with patch.object(self.runner, "_run", **kwargs) as run, \
                     patch("sys.stderr", new_callable=io.StringIO) as log:
                    chat.close()
                    chat.close()
                run.assert_called_once()
                self.assertTrue(chat.closed)
                self.assertIn("AX task cleanup not confirmed", log.getvalue())
                self.assertIn(chat.container_name, log.getvalue())
                self.assertNotIn("private", log.getvalue())

    def test_failed_ax_task_stops_startup_with_its_status_reason(self):
        missing = subprocess.CompletedProcess([], 1, "", "rpc error: code = NotFound")
        applied = subprocess.CompletedProcess([], 0, "", "")
        described = subprocess.CompletedProcess([], 0,
            "Name: popot-chat-0123456789abcdef\nPhase: Failed\nConditions:\n"
            "  TYPE STATUS REASON MESSAGE\n  Ready False ImagePullBackOff image unavailable\n", "")
        deleted = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(self.runner, "_run",
                          side_effect=[missing, applied, applied, described, deleted]) as run, \
             patch("ax_local.api.ax_runner.AxChat.is_alive", return_value=False), \
             patch("ax_local.api.ax_runner.time.monotonic", side_effect=[0, 301]), \
             patch("ax_local.api.ax_runner.time.sleep"):
            with self.assertRaisesRegex(AgentRunError, "Failed.*ImagePullBackOff"):
                self.runner.start_chat("0123456789abcdef")
        self.assertEqual(run.call_args_list[3].args[:3],
                         ("describe", "task", "popot-chat-0123456789abcdef"))

    def test_large_restore_is_chunked_for_ax_ssh_argument_limit(self):
        response = subprocess.CompletedProcess([], 0, 'POPOT_AX_RESPONSE:{"status":"ok"}\n', '')
        with patch.object(self.runner, "_run", return_value=response) as run:
            result = self.runner.remote("popot-chat-0123456789abcdef",
                                        {"action": "restore", "messages": [{
                                            "role": "user", "content": "a" * 100000,
                                        }]})
        self.assertEqual(result, {"status": "ok"})
        self.assertGreater(len(run.call_args_list), 2)
        self.assertTrue(all(len(arg) < 60000 for call in run.call_args_list
                            for arg in call.args if isinstance(arg, str)))


if __name__ == "__main__":
    unittest.main()

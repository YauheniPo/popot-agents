import json
import subprocess
import unittest
from unittest.mock import patch

from ax_local.config import AX_CONFIG
from ax_local.api.ax_runner import AxAgentRunner, AxChat
from popot_agents.orchestrator.main import AgentRunError


class AxRunnerTests(unittest.TestCase):
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

    def test_existing_task_is_replaced_after_api_restart(self):
        present = subprocess.CompletedProcess([], 0, "", "")
        deleted = subprocess.CompletedProcess([], 0, "", "")
        applied = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(self.runner, "_run", side_effect=[present, deleted, applied, applied]) as run, \
             patch.object(self.runner, "remote", return_value={"status": "ok"}):
            self.runner.start_chat("0123456789abcdef")
        self.assertEqual(run.call_args_list[1].args[:3],
                         ("delete", "task", "popot-chat-0123456789abcdef"))

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

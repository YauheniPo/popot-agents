import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class RuntimeConfigTests(unittest.TestCase):
    def test_default_config_collects_runtime_limits_and_timeouts(self):
        from popot_agents.runtime_config import DEFAULT_PATH, load_runtime_config

        declared = json.loads(DEFAULT_PATH.read_text())
        settings = load_runtime_config(DEFAULT_PATH)
        for section, values in declared.items():
            if isinstance(values, dict):
                for name, value in values.items():
                    self.assertEqual(settings[section][name], value, f'{section}.{name}')
            else:
                self.assertEqual(settings[section], values)

    def test_model_retry_config_validates_bounds_and_supports_old_configs(self):
        from popot_agents.runtime_config import load_runtime_config
        settings = load_runtime_config()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'runtime.json'
            settings['model'].pop('request_retries', None)
            path.write_text(json.dumps(settings))
            self.assertEqual(load_runtime_config(path)['model']['request_retries'], 1)
            for value in (0, 1, 3, 4, 10, 100, -1, True, 1.5, "10"):
                settings['model']['request_retries'] = value
                path.write_text(json.dumps(settings))
                if type(value) is int and value >= 0:
                    self.assertEqual(load_runtime_config(path)['model']['request_retries'], value)
                else:
                    with self.assertRaisesRegex(ValueError, 'request_retries'):
                        load_runtime_config(path)

    def test_custom_config_is_loaded_and_bad_values_are_rejected(self):
        from popot_agents.runtime_config import load_runtime_config

        settings = load_runtime_config()
        settings["sessions"]["retention_days"] = 14
        settings["timeouts"]["model_request_seconds"] = 90
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            path.write_text(json.dumps(settings))
            loaded = load_runtime_config(path)
            self.assertEqual(loaded["sessions"]["retention_days"], 14)
            self.assertEqual(loaded["timeouts"]["model_request_seconds"], 90)
            loaded["worker"]["pids_limit"] = 0
            path.write_text(json.dumps(loaded))
            with self.assertRaisesRegex(ValueError, "worker.pids_limit"):
                load_runtime_config(path)
            loaded["worker"]["pids_limit"] = 64
            loaded["limits"]["socket_message_bytes"] = 1024
            path.write_text(json.dumps(loaded))
            with self.assertRaisesRegex(ValueError, "socket_message_bytes"):
                load_runtime_config(path)
            loaded["limits"]["socket_message_bytes"] = 131072
            loaded["worker"]["default_timeout_seconds"] = 301
            path.write_text(json.dumps(loaded))
            with self.assertRaisesRegex(ValueError, "default_timeout_seconds"):
                load_runtime_config(path)
            loaded["worker"]["default_timeout_seconds"] = 60
            loaded["sessions"]["default_idle_seconds"] = 0
            path.write_text(json.dumps(loaded))
            with self.assertRaisesRegex(ValueError, "default_idle_seconds"):
                load_runtime_config(path)

    def test_role_env_names_are_validated_in_runtime_config(self):
        from popot_agents.runtime_config import load_runtime_config

        settings = load_runtime_config()
        settings["role_env_names"] = ["GITHUB_TOKEN", "SERVICE_API_KEY"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            path.write_text(json.dumps(settings))
            self.assertEqual(load_runtime_config(path)["role_env_names"],
                             ["GITHUB_TOKEN", "SERVICE_API_KEY"])
            for value in ("GITHUB_TOKEN", ["GITHUB_TOKEN", "GITHUB_TOKEN"],
                          ["bad-name"], ["HARNESS_MODEL"], ["AX_PROXY_TOKEN"], ["PATH"]):
                with self.subTest(value=value):
                    settings["role_env_names"] = value
                    path.write_text(json.dumps(settings))
                    with self.assertRaisesRegex(ValueError, "role_env_names"):
                        load_runtime_config(path)

    def test_services_use_the_same_custom_runtime_file(self):
        from popot_agents.runtime_config import load_runtime_config

        settings = load_runtime_config()
        settings["sessions"]["retention_days"] = 14
        settings["limits"]["request_bytes"] = 77777
        settings["timeouts"]["model_request_seconds"] = 90
        settings["logging"]["mcp_body_max_bytes"] = 22222
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            path.write_text(json.dumps(settings))
            code = (
                "import json; "
                "from popot_agents.orchestrator.main import MAX_REQUEST_BYTES; "
                "from popot_agents.orchestrator.session_store import SESSION_RETENTION; "
                "from popot_agents.mcp_server import MAX_LOG_BODY_BYTES; "
                "from popot_agents.runtime_config import RUNTIME; "
                "print(json.dumps([MAX_REQUEST_BYTES, SESSION_RETENTION.days, "
                "MAX_LOG_BODY_BYTES, RUNTIME['timeouts']['model_request_seconds']]))"
            )
            result = subprocess.run([sys.executable, "-c", code], text=True,
                                    capture_output=True, check=True,
                                    env={**os.environ, "POPOT_RUNTIME_CONFIG": str(path)})
        self.assertEqual(json.loads(result.stdout), [77777, 14, 22222, 90])

    def test_agent_profile_can_override_resources_and_model_parameters(self):
        from popot_agents.runtime_config import RUNTIME
        from popot_agents.orchestrator.main import DockerAgentRunner
        from popot_agents.worker.http_harness import run_http

        runner = DockerAgentRunner(
            resources={"memory": "2g", "cpus": 2, "pids_limit": 128},
            model_parameters={"temperature": 0.3, "max_tokens": 512},
        )
        command = runner._docker_command("popot-chat-test", detached=True)
        self.assertEqual(command[command.index("--memory") + 1], "2g")
        self.assertEqual(command[command.index("--cpus") + 1], "2")
        self.assertEqual(command[command.index("--pids-limit") + 1], "128")
        self.assertIn('HARNESS_MODEL_PARAMETERS_JSON={"temperature": 0.3, "max_tokens": 512}',
                      command)
        calls = []

        def urlopen(call, timeout):
            calls.append((json.loads(call.data), timeout))
            return io.BytesIO(b'{"choices":[{"message":{"content":"ok"}}]}')

        with patch.dict(os.environ, {
            "HARNESS_BASE_URL": "https://model.example/v1", "HARNESS_MODEL": "test",
            "HARNESS_MODEL_PARAMETERS_JSON": json.dumps(runner.model_parameters),
        }), patch("urllib.request.urlopen", side_effect=urlopen):
            self.assertEqual(run_http("hello"), "ok")
        self.assertEqual(calls[0][0]["temperature"], 0.3)
        self.assertEqual(calls[0][0]["max_tokens"], 512)
        self.assertGreater(calls[0][1], 0)
        self.assertLessEqual(calls[0][1], RUNTIME['timeouts']['model_request_seconds'])

    def test_model_parameters_cannot_override_conversation_payload(self):
        from popot_agents.worker.http_harness import run_http

        with patch.dict(os.environ, {
            "HARNESS_BASE_URL": "https://model.example/v1", "HARNESS_MODEL": "test",
            "HARNESS_MODEL_PARAMETERS_JSON": '{"messages":[]}',
        }), patch("urllib.request.urlopen") as urlopen:
            with self.assertRaisesRegex(ValueError, "model parameters"):
                run_http("hello")
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()

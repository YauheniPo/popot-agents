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
    def test_expanded_budget_reaches_worker_and_ax_transport(self):
        from popot_agents.runtime_config import RUNTIME
        from popot_agents.orchestrator.main import DockerAgentRunner, load_agents
        from ax_local.config import AX_CONFIG
        command = DockerAgentRunner()._docker_command('budget-test', False)
        self.assertIn('HARNESS_TURN_TIMEOUT_SECONDS=885', command)
        self.assertEqual(command[command.index('--memory') + 1], '1536m')
        self.assertEqual(float(command[command.index('--cpus') + 1]), 2)
        self.assertEqual(RUNTIME['worker']['max_concurrent_tasks'], 3)
        self.assertGreater(AX_CONFIG['ax']['route_timeout_seconds'],
                           RUNTIME['worker']['max_timeout_seconds'] + AX_CONFIG['ax']['response_margin_seconds'])
        agents_path = Path(__file__).resolve().parents[1] / 'config/agents.json'
        for runner in load_agents(str(agents_path)).values():
            self.assertEqual(runner.timeout_seconds, RUNTIME['worker']['default_timeout_seconds'])

    def test_ax_mcp_inherits_shared_transport_budget(self):
        compose = Path(__file__).resolve().parents[1] / 'ax_local/compose.yaml'
        self.assertNotIn('MCP_UPSTREAM_TIMEOUT_SECONDS:', compose.read_text())

    def test_shared_worker_budget_applies_to_docker_and_ax(self):
        from popot_agents.runtime_config import RUNTIME
        from popot_agents.orchestrator.main import DockerAgentRunner
        from ax_local.api.ax_runner import AxAgentRunner
        with patch.dict(RUNTIME['worker'], {'response_margin_seconds': 9}):
            command = DockerAgentRunner()._docker_command('test', False,
                role_config={'timeout_seconds': 120})
            self.assertIn('HARNESS_TURN_TIMEOUT_SECONDS=111', command)
            runner = AxAgentRunner("worker@sha256:" + "a" * 64, "test", "http://model/v1")
            manifest = runner.task_manifest('0123456789abcdef', {'timeout_seconds': 120})
            env = {item['name']: item['value'] for item in manifest['spec']['env']}
            self.assertEqual(env['HARNESS_TURN_TIMEOUT_SECONDS'], '111')

    def test_shared_final_answer_reserve_changes_runtime_behavior(self):
        from popot_agents.runtime_config import RUNTIME
        from popot_agents.worker.http_harness import run_http
        now = [0.0]
        requests = []
        def respond(call, timeout):
            requests.append(json.loads(call.data))
            if len(requests) == 1:
                now[0] = 44.0
                return io.BytesIO(b'{"choices":[{"message":{"tool_calls":[{"id":"x","function":{"name":"utc_time","arguments":"{}"}}]}}]}')
            return io.BytesIO(b'{"choices":[{"message":{"content":"done"}}]}')
        with patch.dict(RUNTIME['model'], {'final_answer_reserve_seconds': 10}), \
             patch.dict(os.environ, {'HARNESS_BASE_URL':'http://model/v1', 'HARNESS_MODEL':'test',
                                    'HARNESS_TURN_TIMEOUT_SECONDS':'60'}, clear=True), \
             patch('popot_agents.worker.http_harness.time.monotonic', side_effect=lambda: now[0]), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=respond), \
             patch('popot_agents.worker.http_harness.execute_tool', return_value='ok') as tool:
            self.assertEqual(run_http('task', {'tools':['utc_time']}), 'done')
        tool.assert_called_once()
        self.assertIn('tools', requests[-1])

    def test_memory_budget_validation_rejects_impossible_capture_and_nonfinite_timeouts(self):
        from popot_agents.runtime_config import load_runtime_config
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'runtime.json'
            for field, value in [('memory_summary_seconds', float('inf')),
                                 ('memory_capture_margin_seconds', load_runtime_config()['timeouts']['memory_capture_min_seconds'])]:
                settings = load_runtime_config()
                settings['timeouts'][field] = value
                path.write_text(json.dumps(settings))
                with self.subTest(field=field), self.assertRaises(ValueError):
                    load_runtime_config(path)

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
            loaded["limits"]["socket_message_bytes"] = settings["limits"]["socket_message_bytes"]
            loaded["worker"]["default_timeout_seconds"] = loaded["worker"]["max_timeout_seconds"] + 1
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

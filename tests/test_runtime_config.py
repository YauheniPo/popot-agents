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
    def test_mcp_transport_and_retry_settings_validate(self):
        from popot_agents.runtime_config import load_runtime_config

        settings = load_runtime_config()
        settings['mcp'] = {'discovery_attempts': 1, 'discovery_backoff_seconds': .25}
        settings['timeouts'].update(mcp_http_seconds=11.5, mcp_sse_read_seconds=45.25)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'runtime.json'
            path.write_text(json.dumps(settings))
            self.assertEqual(load_runtime_config(path), settings)
            for section, field, invalid_values in (
                    ('mcp', 'discovery_attempts', (0, -1, True, 1.5, '3')),
                    ('mcp', 'discovery_backoff_seconds', (0, -1, True, '1', float('inf'), float('nan'))),
                    ('timeouts', 'mcp_http_seconds', (0, -1, True, float('inf'))),
                    ('timeouts', 'mcp_sse_read_seconds', (0, -1, True, float('nan')))):
                for value in invalid_values:
                    invalid = json.loads(json.dumps(settings))
                    invalid[section][field] = value
                    path.write_text(json.dumps(invalid))
                    with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, field):
                        load_runtime_config(path)

    def test_ax_budgets_validate_against_same_custom_runtime(self):
        from popot_agents.runtime_config import load_runtime_config
        from ax_local.config import load_ax_config

        runtime = load_runtime_config()
        runtime['timeouts']['max_turn_seconds'] = 2000
        settings = load_ax_config()
        settings['route_timeout_seconds'] = 1920
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ax.json'
            path.write_text(json.dumps(settings))
            with self.assertRaisesRegex(ValueError, 'route_timeout_seconds'):
                load_ax_config(path, runtime=runtime)
            settings['route_timeout_seconds'] = 2100
            path.write_text(json.dumps(settings))
            self.assertEqual(load_ax_config(path, runtime=runtime)['route_timeout_seconds'], 2100)

    def test_expanded_budget_reaches_worker_and_ax_transport(self):
        from popot_agents.runtime_config import RUNTIME
        from ax_local.api.ax_runner import AxAgentRunner
        from ax_local.config import AX_CONFIG
        runner = AxAgentRunner('worker@sha256:' + 'a' * 64, 'test', 'http://model/v1')
        manifest = runner.task_manifest('0123456789abcdef')
        env = {item['name']: item['value'] for item in manifest['spec']['env']}
        self.assertEqual(env['HARNESS_TURN_TIMEOUT_SECONDS'], '885')
        self.assertEqual(manifest['spec']['resources'], AX_CONFIG['task_resources'])
        self.assertGreater(AX_CONFIG['route_timeout_seconds'],
                           RUNTIME['timeouts']['max_turn_seconds'] + AX_CONFIG['response_margin_seconds'])

    def test_ax_mcp_inherits_shared_transport_budget(self):
        compose = Path(__file__).resolve().parents[1] / 'ax_local/compose.yaml'
        self.assertNotIn('MCP_UPSTREAM_TIMEOUT_SECONDS:', compose.read_text())

    def test_mcp_transport_covers_ax_queue_startup_and_model_turn(self):
        from popot_agents.runtime_config import RUNTIME
        from ax_local.config import AX_CONFIG
        ax = AX_CONFIG
        required = (ax['worker_queue_timeout_seconds'] + ax['startup_timeout_seconds']
                    + RUNTIME['timeouts']['max_turn_seconds'] + 3 * ax['cli_timeout_seconds']
                    + ax['restore_timeout_seconds'] + ax['response_margin_seconds'])
        self.assertGreater(RUNTIME['timeouts']['mcp_upstream_seconds'], required)

    def test_shared_worker_budget_applies_to_ax(self):
        from popot_agents.runtime_config import RUNTIME
        from ax_local.api.ax_runner import AxAgentRunner
        with patch.dict(RUNTIME['timeouts'], {'turn_response_margin_seconds': 9}):
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
            answer = run_http('task', {'tools':['utc_time']})
        self.assertIn('Partial result', answer)
        self.assertTrue(answer.endswith('done'))
        tool.assert_called_once()
        self.assertNotIn('tools', requests[-1])
        self.assertEqual(requests[-1]['tool_choice'], 'none')

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

    def test_model_retry_config_is_explicit_and_validates_bounds(self):
        from popot_agents.runtime_config import load_runtime_config
        settings = load_runtime_config()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'runtime.json'
            settings['model'].pop('request_retries', None)
            path.write_text(json.dumps(settings))
            with self.assertRaisesRegex(ValueError, 'model fields'):
                load_runtime_config(path)
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
            loaded["limits"]["socket_message_bytes"] = 1024
            path.write_text(json.dumps(loaded))
            with self.assertRaisesRegex(ValueError, "socket_message_bytes"):
                load_runtime_config(path)
            loaded["limits"]["socket_message_bytes"] = settings["limits"]["socket_message_bytes"]
            loaded["timeouts"]["turn_seconds"] = loaded["timeouts"]["max_turn_seconds"] + 1
            path.write_text(json.dumps(loaded))
            with self.assertRaisesRegex(ValueError, "turn_seconds"):
                load_runtime_config(path)
            loaded["timeouts"]["turn_seconds"] = 60
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

    def test_ax_profile_model_parameters_reach_harness(self):
        from popot_agents.runtime_config import RUNTIME
        from popot_agents.worker.http_harness import run_http

        from ax_local.api.ax_runner import AxAgentRunner
        runner = AxAgentRunner('worker@sha256:' + 'a' * 64, 'test', 'http://model/v1',
                              model_parameters={"temperature": 0.3, "max_tokens": 512})
        manifest = runner.task_manifest('0123456789abcdef')
        env = {item['name']: item['value'] for item in manifest['spec']['env']}
        calls = []

        def urlopen(call, timeout):
            calls.append((json.loads(call.data), timeout))
            return io.BytesIO(b'{"choices":[{"message":{"content":"ok"}}]}')

        with patch.dict(os.environ, {
            "HARNESS_BASE_URL": "https://model.example/v1", "HARNESS_MODEL": "test",
            "HARNESS_MODEL_PARAMETERS_JSON": env["HARNESS_MODEL_PARAMETERS_JSON"],
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

    def test_reasoning_parameters_reach_model(self):
        from ax_local.api.ax_runner import AxAgentRunner
        from popot_agents.worker.http_harness import run_http

        for reasoning in ({'effort': 'high', 'exclude': True},
                          {'max_tokens': 2048, 'exclude': False},
                          {'enabled': True}, {'enabled': False}):
            with self.subTest(reasoning=reasoning):
                parameters = {'reasoning': reasoning, 'max_tokens': 4096}
                runner = AxAgentRunner('worker@sha256:' + 'a' * 64, 'test', 'http://model/v1',
                                       model_parameters=parameters)
                manifest = runner.task_manifest('0123456789abcdef')
                env = {item['name']: item['value'] for item in manifest['spec']['env']}
                calls = []

                def respond(call, timeout):
                    calls.append(json.loads(call.data))
                    return io.BytesIO(b'{"choices":[{"message":{"content":"ok"}}]}')

                with patch.dict(os.environ, {
                    'HARNESS_BASE_URL': 'http://model.test/v1', 'HARNESS_MODEL': 'test',
                    'HARNESS_MODEL_PARAMETERS_JSON': env['HARNESS_MODEL_PARAMETERS_JSON'],
                }, clear=True), patch('urllib.request.urlopen', side_effect=respond):
                    self.assertEqual(run_http('hello'), 'ok')
                self.assertEqual(calls[0]['reasoning'], reasoning)
                self.assertEqual(calls[0]['max_tokens'], 4096)
                self.assertEqual(calls[0]['messages'], [{'role': 'user', 'content': 'hello'}])

    def test_invalid_reasoning_is_rejected_before_model_request(self):
        from popot_agents.worker.http_harness import run_http

        for reasoning in (None, True, 'high', [], {'effort': 'invalid'}, {'effort': []},
                          {'max_tokens': 0}, {'max_tokens': True}, {'max_tokens': 1.5},
                          {'enabled': 'true'}, {'exclude': 1}, {'messages': []},
                          {'effort': 'high', 'max_tokens': 2048}):
            with self.subTest(reasoning=reasoning), patch.dict(os.environ, {
                'HARNESS_BASE_URL': 'http://model.test/v1', 'HARNESS_MODEL': 'test',
                'HARNESS_MODEL_PARAMETERS_JSON': json.dumps({'reasoning': reasoning}),
            }, clear=True), patch('urllib.request.urlopen') as urlopen:
                with self.assertRaisesRegex(ValueError, 'reasoning'):
                    run_http('hello')
                urlopen.assert_not_called()

    def test_nvidia_thinking_parameters_reach_model(self):
        from ax_local.api.ax_runner import AxAgentRunner
        from popot_agents.worker.http_harness import run_http

        for enabled in (True, False):
            parameters = {'chat_template_kwargs': {'enable_thinking': enabled}}
            with self.subTest(enabled=enabled):
                runner = AxAgentRunner('worker@sha256:' + 'a' * 64, 'test', 'http://model/v1',
                                       model_parameters=parameters)
                manifest = runner.task_manifest('0123456789abcdef')
                env = {item['name']: item['value'] for item in manifest['spec']['env']}
                calls = []

                def respond(call, timeout):
                    calls.append(json.loads(call.data))
                    return io.BytesIO(b'{"choices":[{"message":{"content":"ok"}}]}')

                with patch.dict(os.environ, {
                    'HARNESS_BASE_URL': 'http://model.test/v1', 'HARNESS_MODEL': 'test',
                    'HARNESS_MODEL_PARAMETERS_JSON': env['HARNESS_MODEL_PARAMETERS_JSON'],
                }, clear=True), patch('urllib.request.urlopen', side_effect=respond):
                    self.assertEqual(run_http('hello'), 'ok')
                self.assertEqual(calls[0]['chat_template_kwargs'], parameters['chat_template_kwargs'])
                self.assertNotIn('reasoning', calls[0])
                self.assertEqual(calls[0]['messages'], [{'role': 'user', 'content': 'hello'}])

    def test_invalid_chat_template_kwargs_are_rejected_before_model_request(self):
        from popot_agents.worker.http_harness import run_http

        for options in (None, True, [], 'thinking', {'enable_thinking': 'true'},
                        {'enable_thinking': 1}, {'enable_thinking': None},
                        {'messages': []}, {'tools': []}):
            with self.subTest(options=options), patch.dict(os.environ, {
                'HARNESS_BASE_URL': 'http://model.test/v1', 'HARNESS_MODEL': 'test',
                'HARNESS_MODEL_PARAMETERS_JSON': json.dumps({'chat_template_kwargs': options}),
            }, clear=True), patch('urllib.request.urlopen') as urlopen:
                with self.assertRaisesRegex(ValueError, 'chat_template_kwargs'):
                    run_http('hello')
                urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()

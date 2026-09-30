import json
import io
import os
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from popot_agents.worker.agent_worker import run_harness
from popot_agents.orchestrator.main import AgentConfigurationError, DockerAgentRunner, create_server, load_agents, load_dotenv


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.received = []

        def run(task):
            self.received.append(task)
            return {"answer": "done: " + task}

        self.server = create_server({"test_agent": run, "other": run}, host="127.0.0.1", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, payload):
        connection = HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request(
            "POST", "/tasks", body=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        data = json.loads(response.read())
        connection.close()
        return response.status, data

    def test_task_returns_agent_answer(self):
        status, data = self.request({"agent": "test_agent", "task": "check the build"})
        self.assertEqual(status, 200)
        self.assertEqual(data, {"answer": "done: check the build"})
        self.assertEqual(self.received, ["check the build"])

    def test_task_requires_agent_or_role(self):
        status, data = self.request({"task": "check the build"})
        self.assertEqual(status, 400)
        self.assertIn("agent or role", data["error"])
        self.assertEqual(self.received, [])

    def test_empty_task_is_rejected_before_launch(self):
        status, data = self.request({"task": "  "})
        self.assertEqual(status, 400)
        self.assertIn("task", data["error"])
        self.assertEqual(self.received, [])

    def test_selects_a_configured_agent(self):
        status, data = self.request({"agent": "other", "task": "work"})
        self.assertEqual(status, 200)
        self.assertEqual(data, {"answer": "done: work"})

    def test_rejects_unknown_agent(self):
        status, data = self.request({"agent": "unconfigured", "task": "work"})
        self.assertEqual(status, 404)
        self.assertIn("agent", data["error"])
        self.assertEqual(self.received, [])

    def test_missing_profile_configuration_returns_503(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.server = create_server({"test_agent": lambda task: (_ for _ in ()).throw(
            AgentConfigurationError("missing provider configuration"))}, host="127.0.0.1", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        status, data = self.request({"agent": "test_agent", "task": "work"})
        self.assertEqual(status, 503)
        self.assertIn("configuration", data["error"])

class RequestLoggingTests(unittest.TestCase):
    @patch("popot_agents.orchestrator.main.ChatServer")
    def test_logs_health_request_and_response(self, server_class):
        create_server({})
        handler_class = server_class.call_args.args[1]
        handler = handler_class.__new__(handler_class)
        handler.client_address = ("192.0.2.8", 2345)
        handler.command = "GET"
        handler.path = "/healthz"
        handler.request_version = "HTTP/1.1"
        handler.requestline = "GET /healthz HTTP/1.1"
        handler.server = SimpleNamespace()
        handler.headers = Message()
        handler.wfile = io.BytesIO()

        output = io.StringIO()
        with redirect_stderr(output):
            handler.do_GET()

        events = [json.loads(line) for line in output.getvalue().splitlines()
                  if line.startswith("{")]
        self.assertEqual([event["event"] for event in events], ["request", "response"])
        self.assertIsNone(events[0]["body"])
        self.assertEqual(events[1]["body"], {"status": "ok"})
        self.assertEqual(events[0]["request_id"], events[1]["request_id"])

    @patch("popot_agents.orchestrator.main.ChatServer")
    def test_logs_full_task_exchange_with_client_and_redacts_credentials(self, server_class):
        create_server({"test_agent": lambda task: {"answer": "готово: " + task}})
        handler_class = server_class.call_args.args[1]
        handler = handler_class.__new__(handler_class)
        handler.client_address = ("192.0.2.7", 1234)
        handler.command = "POST"
        handler.path = "/tasks"
        handler.request_version = "HTTP/1.1"
        handler.requestline = "POST /tasks HTTP/1.1"
        handler.server = SimpleNamespace()
        handler.headers = Message()
        body = json.dumps({"agent": "test_agent", "task": "проверить каталог",
                           "api_key": "private-value",
                           "OPENROUTER_API_KEY": "provider-secret"}, ensure_ascii=False).encode()
        handler.headers["Content-Type"] = "application/json"
        handler.headers["Content-Length"] = str(len(body))
        handler.headers["User-Agent"] = "test-client/1.0"
        handler.headers["Authorization"] = "Bearer private-header"
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()

        output = io.StringIO()
        with redirect_stderr(output):
            handler.do_POST()

        events = [json.loads(line) for line in output.getvalue().splitlines()
                  if line.startswith("{")]
        self.assertEqual([event["event"] for event in events], ["request", "response"])
        self.assertEqual(events[0]["client_ip"], "192.0.2.7")
        self.assertEqual(events[0]["user_agent"], "test-client/1.0")
        self.assertEqual(events[0]["method"], "POST")
        self.assertEqual(events[0]["path"], "/tasks")
        self.assertEqual(events[0]["body"], {"agent": "test_agent", "task": "проверить каталог",
                                             "api_key": "[REDACTED]",
                                             "OPENROUTER_API_KEY": "[REDACTED]"})
        self.assertEqual(events[1]["status"], 200)
        self.assertEqual(events[1]["client_ip"], "192.0.2.7")
        self.assertEqual(events[1]["method"], "POST")
        self.assertEqual(events[1]["path"], "/tasks")
        self.assertEqual(events[1]["body"], {"answer": "готово: проверить каталог"})
        self.assertEqual(events[0]["request_id"], events[1]["request_id"])
        self.assertNotIn("private-value", output.getvalue())
        self.assertNotIn("provider-secret", output.getvalue())
        self.assertNotIn("private-header", output.getvalue())


class DockerRunnerTests(unittest.TestCase):
    @patch("popot_agents.orchestrator.main.subprocess.run")
    def test_launches_isolated_container_and_reads_result(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = '{"answer":"finished"}\n'
        run.return_value.stderr = ""
        runner = DockerAgentRunner(image="popot-agent-worker:local", timeout_seconds=20)

        self.assertEqual(runner("summarize this"), {"answer": "finished"})

        args = run.call_args.args[0]
        self.assertEqual(args[:3], ["docker", "run", "--rm"])
        self.assertIn("--memory", args)
        self.assertIn("--cpus", args)
        self.assertIn("HOME=/workspace", args)
        self.assertIn("popot-agent-worker:local", args)
        self.assertNotIn("sh", args)
        self.assertEqual(json.loads(run.call_args.kwargs["input"]), {"task": "summarize this"})
        self.assertEqual(run.call_args.kwargs["timeout"], 20)

    @patch("popot_agents.orchestrator.main.subprocess.run")
    def test_timeout_removes_container(self, run):
        run.side_effect = [
            subprocess.TimeoutExpired(["docker", "run"], 20),
            subprocess.CompletedProcess(["docker", "rm"], 0),
        ]
        runner = DockerAgentRunner(image="popot-agent-worker:local", timeout_seconds=20)

        with self.assertRaises(subprocess.TimeoutExpired):
            runner("slow task")

        self.assertEqual(run.call_count, 2)
        launch = run.call_args_list[0].args[0]
        cleanup = run.call_args_list[1].args[0]
        self.assertIn("--name", launch)
        self.assertEqual(cleanup[:3], ["docker", "rm", "-f"])
        self.assertEqual(cleanup[3], launch[launch.index("--name") + 1])

    @patch("popot_agents.orchestrator.main.subprocess.run")
    def test_timeout_logs_worker_progress_without_task_or_key(self, run):
        run.side_effect = [
            subprocess.TimeoutExpired(["docker", "run"], 20),
            subprocess.CompletedProcess(["docker", "rm"], 0),
        ]
        runner = DockerAgentRunner(image="popot-agent-worker:local", timeout_seconds=20,
                                   env_names=["OPENROUTER_API_KEY"], model_env="OPENROUTER_MODEL")
        output = io.StringIO()
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "private-value",
                                  "OPENROUTER_MODEL": "openrouter/free"}), redirect_stderr(output):
            with self.assertRaises(subprocess.TimeoutExpired):
                runner("private prompt")
        logs = output.getvalue()
        self.assertIn("OPENROUTER_MODEL", logs)
        self.assertIn("timed out", logs)
        self.assertNotIn("openrouter/free", logs)
        self.assertNotIn("private-value", logs)
        self.assertNotIn("private prompt", logs)

    @patch("popot_agents.orchestrator.main.subprocess.run")
    def test_operator_can_enable_network_and_forward_named_credential(self, run):
        run.return_value = subprocess.CompletedProcess(["docker", "run"], 0, '{"answer":"ok"}', "")
        with patch.dict(os.environ, {"EXAMPLE_API_KEY": "private-value"}):
            runner = DockerAgentRunner(
                image="example:local", network="bridge", env_names=["EXAMPLE_API_KEY"]
            )
            runner("task")

        args = run.call_args.args[0]
        self.assertEqual(args[args.index("--network") + 1], "bridge")
        self.assertIn("EXAMPLE_API_KEY", args)
        self.assertNotIn("private-value", args)

    @patch("popot_agents.orchestrator.main.subprocess.run")
    def test_configured_command_and_model_are_passed_to_worker(self, run):
        run.return_value = subprocess.CompletedProcess(["docker", "run"], 0, '{"answer":"ok"}', "")
        with patch.dict(os.environ, {"OPENROUTER_MODEL": "example/model", "OPENROUTER_API_KEY": "secret"}):
            runner = DockerAgentRunner(
                network="bridge", command=["python", "-m", "popot_agents.worker.http_harness"],
                environment={"HARNESS_BASE_URL": "https://example.test/v1", "HARNESS_API_KEY_ENV": "OPENROUTER_API_KEY"},
                env_names=["OPENROUTER_API_KEY"], model_env="OPENROUTER_MODEL",
            )
            runner("work")
        args = run.call_args.args[0]
        self.assertIn("HARNESS_MODEL=example/model", args)
        self.assertIn('HARNESS_COMMAND_JSON=["python", "-m", "popot_agents.worker.http_harness"]', args)
        self.assertIn("OPENROUTER_API_KEY", args)
        self.assertNotIn("secret", args)

    def test_missing_required_model_fails_when_profile_is_used(self):
        with patch.dict(os.environ, {}, clear=True):
            runner = DockerAgentRunner(model_env="MISSING_MODEL")
            with self.assertRaisesRegex(AgentConfigurationError, "MISSING_MODEL"):
                runner("work")

    def test_profiles_file_loads_named_runners(self):
        agents = load_agents(Path(__file__).resolve().parents[1] / "config" / "agents.json")
        self.assertIn("openrouter", agents)
        self.assertIn("ollama", agents)
        self.assertIn("ollama_cloud", agents)
        self.assertIn("ollama_claude", agents)
        self.assertIn("ollama_claude_local", agents)
        self.assertIn("nous", agents)
        self.assertEqual(agents["nous"].env_names, ["NOUS_API_KEY"])
        self.assertEqual(agents["nous"].environment["HARNESS_API_KEY_ENV"], "NOUS_API_KEY")
        self.assertEqual(agents["nous"].session_mode, "http")
        self.assertIn("nvidia_nim", agents)
        self.assertEqual(agents["ollama_claude"].environment["ANTHROPIC_BASE_URL"], "https://ollama.com")
        self.assertEqual(agents["ollama_claude"].env_names, ["OLLAMA_API_KEY"])
        self.assertEqual(agents["ollama_claude"].env_aliases, {"ANTHROPIC_AUTH_TOKEN": "OLLAMA_API_KEY"})
        self.assertEqual(agents["ollama"].environment["HARNESS_BASE_URL"], "http://host.docker.internal:11434/v1")
        self.assertEqual(agents["ollama_cloud"].env_names, ["OLLAMA_API_KEY"])

    def test_profiles_file_does_not_require_a_special_profile(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as file:
            json.dump({"custom": {"image": "custom:local"}}, file)
            path = file.name
        try:
            self.assertEqual(load_agents(path)["custom"].image, "custom:local")
        finally:
            os.unlink(path)

    def test_dotenv_loads_values_without_overwriting_process_environment(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as file:
            file.write('OPENROUTER_API_KEY="from-file"\nexport OLLAMA_MODEL=qwen2.5-coder:14b\n# ignored\n')
            path = file.name
        try:
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "from-process"}, clear=True):
                load_dotenv(path)
                self.assertEqual(os.environ["OPENROUTER_API_KEY"], "from-process")
                self.assertEqual(os.environ["OLLAMA_MODEL"], "qwen2.5-coder:14b")
        finally:
            os.unlink(path)


class HarnessWorkerTests(unittest.TestCase):
    @patch("popot_agents.worker.agent_worker.subprocess.run")
    def test_passes_task_to_configured_cli_and_returns_its_answer(self, run):
        run.return_value = subprocess.CompletedProcess(["example-cli"], 0, "completed\n", "")
        with patch.dict(os.environ, {"HARNESS_COMMAND_JSON": '["example-cli", "--print"]'}):
            answer = run_harness("Investigate the failure")

        self.assertEqual(answer, "completed")
        self.assertEqual(run.call_args.args[0], ["example-cli", "--print"])
        self.assertEqual(run.call_args.kwargs["input"], "Investigate the failure")
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    @patch("popot_agents.worker.agent_worker.subprocess.run")
    def test_cli_failure_is_not_reported_as_an_answer(self, run):
        run.return_value = subprocess.CompletedProcess(["example-cli"], 1, "", "failed")
        with patch.dict(os.environ, {"HARNESS_COMMAND_JSON": '["example-cli"]'}):
            with self.assertRaisesRegex(RuntimeError, "harness exited"):
                run_harness("Investigate the failure")

    @patch("popot_agents.worker.agent_worker.subprocess.run")
    def test_cli_can_take_task_as_argument(self, run):
        run.return_value = subprocess.CompletedProcess(["example-cli"], 0, "answer\n", "")
        with patch.dict(os.environ, {"HARNESS_COMMAND_JSON": '["example-cli", "--oneshot", "{task}"]'}):
            answer = run_harness("Explain the build")

        self.assertEqual(answer, "answer")
        self.assertEqual(
            run.call_args.args[0], ["example-cli", "--oneshot", "Explain the build"]
        )
        self.assertEqual(run.call_args.kwargs["input"], "")

    @patch("popot_agents.worker.agent_worker.subprocess.run")
    def test_cli_command_can_use_selected_model(self, run):
        run.return_value = subprocess.CompletedProcess(["claude"], 0, "answer\n", "")
        with patch.dict(os.environ, {"HARNESS_COMMAND_JSON": '["claude", "--model", "{model}", "{task}"]', "HARNESS_MODEL": "qwen3"}):
            self.assertEqual(run_harness("work"), "answer")
        self.assertEqual(run.call_args.args[0], ["claude", "--model", "qwen3", "work"])

    @patch("popot_agents.worker.agent_worker.subprocess.run")
    def test_cli_receives_named_token_alias_without_command_argument(self, run):
        run.return_value = subprocess.CompletedProcess(["claude"], 0, "answer\n", "")
        with patch.dict(os.environ, {
            "HARNESS_COMMAND_JSON": '["claude", "--print", "{task}"]',
            "HARNESS_ENV_ALIASES_JSON": '{"ANTHROPIC_AUTH_TOKEN":"OLLAMA_API_KEY"}',
            "OLLAMA_API_KEY": "private-value",
        }):
            self.assertEqual(run_harness("work"), "answer")
        self.assertEqual(run.call_args.kwargs["env"]["ANTHROPIC_AUTH_TOKEN"], "private-value")
        self.assertNotIn("private-value", run.call_args.args[0])


class HttpHarnessTests(unittest.TestCase):
    @patch("popot_agents.worker.http_harness.request.urlopen")
    def test_turn_deadline_limits_model_rounds(self, urlopen):
        from popot_agents.worker.http_harness import run_http

        clock = [0.0]
        tool_reply = {"choices": [{"message": {"tool_calls": [{
            "id": "call-1", "type": "function", "function": {
                "name": "calculate", "arguments": '{"expression":"1+1"}'}}]}}]}

        def reply(_request, timeout):
            self.assertLessEqual(timeout, 1.0 - clock[0])
            clock[0] += 0.6
            return io.BytesIO(json.dumps(tool_reply).encode())

        urlopen.side_effect = reply
        tool_timeouts = []

        def execute(_name, _arguments, _allowed, *, timeout_seconds):
            tool_timeouts.append(timeout_seconds)
            return "2"

        with patch.dict(os.environ, {"HARNESS_BASE_URL": "https://example.test/v1",
                                  "HARNESS_MODEL": "example-model",
                                  "HARNESS_TURN_TIMEOUT_SECONDS": "1"}), \
                patch("popot_agents.worker.http_harness.time.monotonic",
                      side_effect=lambda: clock[0]), \
                patch("popot_agents.worker.http_harness.execute_tool", side_effect=execute), \
                redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "turn deadline exceeded"):
                run_http("calculate", {"tools": ["calculate"], "max_tool_rounds": 3})
        self.assertEqual(urlopen.call_count, 2)
        self.assertTrue(all(0 < value <= 0.4 for value in tool_timeouts))

    @patch("popot_agents.worker.http_harness.request.urlopen")
    def test_logs_visible_model_steps_and_tool_calls(self, urlopen):
        from popot_agents.worker.http_harness import run_http

        tool_message = {"choices": [{"message": {
            "content": "Проверю выражение.",
            "tool_calls": [{"id": "call-1", "type": "function", "function": {
                "name": "calculate", "arguments": '{"expression":"2+3"}'}}],
        }}]}
        answer_message = {"choices": [{"message": {"content": "Ответ: 5"}}]}
        urlopen.side_effect = [io.BytesIO(json.dumps(tool_message).encode()),
                               io.BytesIO(json.dumps(answer_message).encode())]
        output = io.StringIO()
        with patch.dict(os.environ, {"HARNESS_BASE_URL": "https://example.test/v1",
                                  "HARNESS_MODEL": "example-model"}), redirect_stderr(output):
            self.assertEqual(run_http("посчитай", {"tools": ["calculate"]}), "Ответ: 5")

        events = [json.loads(line) for line in output.getvalue().splitlines()
                  if line.startswith("{")]
        self.assertEqual([event["event"] for event in events], [
            "model_request", "model_response", "assistant_message", "tool_call",
            "tool_result", "model_request", "model_response", "assistant_answer"])
        self.assertEqual(events[0]["available_tools"], ["calculate"])
        self.assertEqual(events[1]["tool_calls"], 1)
        self.assertEqual(events[2]["content"], "Проверю выражение.")
        self.assertEqual(events[3]["name"], "calculate")
        self.assertEqual(events[3]["arguments"], {"expression": "2+3"})
        self.assertEqual(events[4]["output"], "5")
        self.assertEqual(events[6]["tool_calls"], 0)
        self.assertEqual(events[7]["content"], "Ответ: 5")

    @patch("popot_agents.worker.http_harness.request.urlopen")
    @patch("popot_agents.worker.http_harness.execute_tool", return_value="private-output")
    def test_tool_trace_omits_shell_command_and_output(self, execute, urlopen):
        from popot_agents.worker.http_harness import run_http

        first = {"choices": [{"message": {"content": None, "tool_calls": [{
            "id": "call-1", "type": "function", "function": {
                "name": "bash", "arguments": '{"command":"echo private-command"}'}}]}}]}
        second = {"choices": [{"message": {"content": "done"}}]}
        urlopen.side_effect = [io.BytesIO(json.dumps(first).encode()),
                               io.BytesIO(json.dumps(second).encode())]
        output = io.StringIO()
        with patch.dict(os.environ, {"HARNESS_BASE_URL": "https://example.test/v1",
                                  "HARNESS_MODEL": "example-model"}), redirect_stderr(output):
            self.assertEqual(run_http("work", {"tools": ["bash"]}), "done")
        events = [json.loads(line) for line in output.getvalue().splitlines()
                  if line.startswith("{")]
        call = next(event for event in events if event["event"] == "tool_call")
        result = next(event for event in events if event["event"] == "tool_result")
        self.assertEqual(call["name"], "bash")
        self.assertIn("command_chars", call["arguments"])
        self.assertIn("output_chars", result)
        self.assertNotIn("private-command", output.getvalue())
        self.assertNotIn("private-output", output.getvalue())

    @patch("popot_agents.worker.http_harness.request.urlopen")
    def test_tool_trace_omits_unexpected_secret_argument(self, urlopen):
        from popot_agents.worker.http_harness import run_http

        message = {"choices": [{"message": {"content": None, "tool_calls": [{
            "id": "call-1", "type": "function", "function": {
                "name": "calculate",
                "arguments": '{"expression":"2+3","token":"private-token"}'}}]}}]}
        urlopen.return_value = io.BytesIO(json.dumps(message).encode())
        output = io.StringIO()
        with patch.dict(os.environ, {"HARNESS_BASE_URL": "https://example.test/v1",
                                  "HARNESS_MODEL": "example-model"}), redirect_stderr(output):
            with self.assertRaisesRegex(RuntimeError, "invalid tool call"):
                run_http("calculate", {"tools": ["calculate"]})
        self.assertNotIn("private-token", output.getvalue())

    @patch("popot_agents.worker.http_harness.request.urlopen")
    def test_logs_provider_progress_without_prompt_or_key(self, urlopen):
        from popot_agents.worker.http_harness import run_http

        urlopen.return_value = io.BytesIO(b'{"choices":[{"message":{"content":"ok"}}]}')
        output = io.StringIO()
        with patch.dict(os.environ, {
            "HARNESS_BASE_URL": "https://example.test/v1",
            "HARNESS_MODEL": "openrouter/free", "HARNESS_API_KEY_ENV": "EXAMPLE_KEY",
            "EXAMPLE_KEY": "private-value",
        }), redirect_stderr(output):
            self.assertEqual(run_http("private prompt"), "ok")
        logs = output.getvalue()
        self.assertIn("LLM request started", logs)
        self.assertIn("round=1", logs)
        self.assertIn("LLM response received", logs)
        self.assertNotIn("openrouter/free", logs)
        self.assertNotIn("private-value", logs)
        self.assertNotIn("private prompt", logs)

    def test_openai_compatible_request_and_answer(self):
        from popot_agents.worker.http_harness import run_http

        received = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received["path"] = self.path
                received["authorization"] = self.headers.get("Authorization")
                received["payload"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                body = b'{"choices":[{"message":{"content":"mock answer"}}]}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(os.environ, {
                "HARNESS_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
                "HARNESS_MODEL": "example-model", "HARNESS_API_KEY_ENV": "EXAMPLE_KEY",
                "EXAMPLE_KEY": "private-value",
            }):
                answer = run_http("explain this")
                followup = run_http([
                    {"role": "user", "content": "first"},
                    {"role": "assistant", "content": "reply"},
                    {"role": "user", "content": "second"},
                ])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual(answer, "mock answer")
        self.assertEqual(followup, "mock answer")
        self.assertEqual(received["path"], "/v1/chat/completions")
        self.assertEqual(received["authorization"], "Bearer private-value")
        self.assertEqual(received["payload"], {
            "model": "example-model", "messages": [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "reply"},
                {"role": "user", "content": "second"},
            ],
        })


if __name__ == "__main__":
    unittest.main()

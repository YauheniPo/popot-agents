import asyncio
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch


class ImagePackagingTests(unittest.TestCase):
    def test_mcp_image_contains_modules_needed_at_import(self):
        project = Path(__file__).resolve().parents[1]
        dockerfile = project / "docker" / "mcp.Dockerfile"
        with tempfile.TemporaryDirectory() as directory:
            image_root = Path(directory)
            for line in dockerfile.read_text().splitlines():
                parts = shlex.split(line)
                if not parts or parts[0] != "COPY":
                    continue
                destination = parts[-1]
                sources = parts[1:-1]
                for source in sources:
                    target = image_root / destination.lstrip("/")
                    if destination.endswith("/") or len(sources) > 1:
                        target /= Path(source).name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(project / source, target)
            result = subprocess.run(
                [sys.executable, "-S", "-c", "import popot_agents.mcp_server"],
                cwd=image_root, env={**os.environ, "PYTHONPATH": str(image_root / "app")},
                text=True, capture_output=True, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.base_url = "http://orchestrator.test:8000"

    def urlopen(self, request, timeout):
        self.assertGreaterEqual(timeout, 180)
        self.assertEqual(request.get_header("User-agent"), "popot-agents-mcp/1")
        path = request.full_url.removeprefix(self.base_url)
        payload = json.loads(request.data) if request.data else None
        self.calls.append((request.get_method(), path, payload))
        if path == "/tasks":
            result = {"answer": "task done", "sessionId": "task123",
                      "container": "popot-chat-task123"}
        elif payload and payload.get("sessionId") == "missing":
            raise HTTPError(request.full_url, 404, "Not Found", {},
                            io.BytesIO(b'{"error":"unknown sessionId"}'))
        elif path == "/messages":
            session_id = payload.get("sessionId", "abc123")
            result = {"answer": "turn done", "sessionId": session_id,
                      "container": f"popot-chat-{session_id}"}
        elif path == "/chats/abc123":
            result = {"sessionId": "abc123", "role": "backend_engineer",
                      "status": "stopped"}
        else:
            result = {"chats": [{"sessionId": "abc123", "role": "backend_engineer"}]}
        return io.BytesIO(json.dumps(result).encode())

    def test_tools_forward_task_and_resume_session_without_creating_new_chat(self):
        from popot_agents.mcp_server import OrchestratorClient

        client = OrchestratorClient(self.base_url)
        with patch("urllib.request.urlopen", side_effect=self.urlopen):
            task = client.run_task("build a feature", role="backend_engineer")
            self.assertEqual(task, {"answer": "task done", "sessionId": "task123",
                                    "container": "popot-chat-task123"})
            task_followup = client.send_message("refine it", session_id=task["sessionId"])
            self.assertEqual(task_followup["sessionId"], task["sessionId"])
            self.assertEqual(task_followup["container"], task["container"])
            first = client.send_message("start", role="backend_engineer")
            resumed = client.send_message("continue", session_id=first["sessionId"])
        self.assertEqual(first["sessionId"], resumed["sessionId"])
        self.assertEqual(first["container"], resumed["container"])
        self.assertEqual(self.calls, [
            ("POST", "/tasks", {"task": "build a feature", "role": "backend_engineer"}),
            ("POST", "/messages", {"message": "refine it", "sessionId": "task123"}),
            ("POST", "/messages", {"message": "start", "role": "backend_engineer"}),
            ("POST", "/messages", {"message": "continue", "sessionId": "abc123"}),
        ])

    def test_run_task_with_session_id_reuses_the_existing_chat(self):
        from popot_agents.mcp_server import OrchestratorClient

        client = OrchestratorClient(self.base_url)
        with patch("urllib.request.urlopen", side_effect=self.urlopen):
            resumed = client.run_task("что еще добавишь?", role="product_manager",
                                      session_id="d7ed630a2ce24092")
        self.assertEqual(resumed["sessionId"], "d7ed630a2ce24092")
        self.assertEqual(resumed["container"], "popot-chat-d7ed630a2ce24092")
        self.assertEqual(self.calls, [("POST", "/messages", {
            "message": "что еще добавишь?", "sessionId": "d7ed630a2ce24092",
            "role": "product_manager",
        })])

    def test_run_task_with_empty_session_id_starts_new_chat(self):
        from popot_agents.mcp_server import OrchestratorClient

        client = OrchestratorClient(self.base_url)
        with patch("urllib.request.urlopen", side_effect=self.urlopen):
            for session_id in ("", "   "):
                with self.subTest(session_id=session_id):
                    result = client.run_task("1+3?", role="product_manager",
                                             session_id=session_id)
                    self.assertEqual(result["sessionId"], "task123")
                    self.assertEqual(result["container"], "popot-chat-task123")
        self.assertEqual(self.calls, [
            ("POST", "/tasks", {"task": "1+3?", "role": "product_manager"}),
            ("POST", "/tasks", {"task": "1+3?", "role": "product_manager"}),
        ])

    def test_tool_errors_keep_orchestrator_status_and_message(self):
        from popot_agents.mcp_server import OrchestratorClient

        client = OrchestratorClient(self.base_url)
        with patch("urllib.request.urlopen", side_effect=self.urlopen):
            with self.assertRaisesRegex(RuntimeError, "404.*unknown sessionId"):
                client.send_message("continue", session_id="missing")

    def test_new_chat_requires_role_or_agent_before_http(self):
        from popot_agents.mcp_server import OrchestratorClient

        client = OrchestratorClient(self.base_url)
        with patch("urllib.request.urlopen", side_effect=self.urlopen):
            with self.assertRaisesRegex(ValueError, "role or agent"):
                client.run_task("2+1?")
            with self.assertRaisesRegex(ValueError, "role or agent"):
                client.send_message("2+1?")
            with self.assertRaisesRegex(ValueError, "role or agent"):
                client.run_task("2+1?", session_id="   ")
            with self.assertRaisesRegex(ValueError, "role must be a nonempty string"):
                client.run_task("2+1?", role="   ")
        self.assertEqual(self.calls, [])

    def test_list_chats_uses_existing_api(self):
        from popot_agents.mcp_server import OrchestratorClient

        client = OrchestratorClient(self.base_url)
        with patch("urllib.request.urlopen", side_effect=self.urlopen):
            self.assertEqual(client.list_chats()["chats"][0]["sessionId"], "abc123")
            self.assertEqual(client.get_chat("abc123")["status"], "stopped")
        self.assertEqual(self.calls, [("GET", "/chats", None),
                                      ("GET", "/chats/abc123", None)])

    def test_server_registers_expected_tools_over_streamable_http(self):
        from popot_agents.mcp_server import main

        class FakeFastMCP:
            instance = None

            def __init__(self, name, **settings):
                self.name = name
                self.settings = settings
                self.tools = {}
                FakeFastMCP.instance = self

            def tool(self):
                def register(function):
                    self.tools[function.__name__] = function
                    return function
                return register

            async def list_tools(self):
                return [types.SimpleNamespace(name=name, inputSchema={
                    "type": "object", "properties": {},
                    "required": ["task" if name == "run_task" else "message"],
                }) for name in ("run_task", "send_message")]

            def run(self, transport):
                self.transport = transport

            def streamable_http_app(self):
                return "upstream-asgi-app"

        fastmcp = types.ModuleType("mcp.server.fastmcp")
        fastmcp.FastMCP = FakeFastMCP
        security = types.ModuleType("mcp.server.transport_security")
        security.TransportSecuritySettings = lambda **settings: settings
        with patch.dict(sys.modules, {
            "mcp": types.ModuleType("mcp"),
            "mcp.server": types.ModuleType("mcp.server"),
            "mcp.server.fastmcp": fastmcp,
            "mcp.server.transport_security": security,
        }), patch.dict(os.environ, {"MCP_ORCHESTRATOR_URL": self.base_url}), \
                patch("urllib.request.urlopen", side_effect=self.urlopen):
            main()
            self.assertEqual(FakeFastMCP.instance.tools["run_task"](
                "build a feature", role="backend_engineer"),
                {"answer": "task done", "sessionId": "task123",
                 "container": "popot-chat-task123"})
            continued = FakeFastMCP.instance.tools["run_task"](
                "что еще добавишь?", role="product_manager",
                sessionId="d7ed630a2ce24092")
            self.assertEqual(continued["sessionId"], "d7ed630a2ce24092")
            self.assertEqual(continued["container"], "popot-chat-d7ed630a2ce24092")
            self.assertEqual(self.calls[-1], ("POST", "/messages", {
                "message": "что еще добавишь?", "sessionId": "d7ed630a2ce24092",
                "role": "product_manager",
            }))
            self.assertEqual(FakeFastMCP.instance.tools["run_task"](
                "continue", session_id="d7ed630a2ce24092")["sessionId"],
                "d7ed630a2ce24092")
            message = FakeFastMCP.instance.tools["send_message"](
                "one more turn", session_id="d7ed630a2ce24092")
            self.assertEqual(message["sessionId"], "d7ed630a2ce24092")
            self.assertEqual(message["container"], "popot-chat-d7ed630a2ce24092")
            self.assertEqual(FakeFastMCP.instance.tools["run_task"](
                "1+3?", role="product_manager", sessionId="")["sessionId"],
                "task123")
            self.assertEqual(self.calls[-1], ("POST", "/tasks", {
                "task": "1+3?", "role": "product_manager",
            }))
            self.assertEqual(FakeFastMCP.instance.tools["run_task"](
                "continue", sessionId="", session_id="d7ed630a2ce24092")["sessionId"],
                "d7ed630a2ce24092")
            with self.assertRaisesRegex(ValueError, "must match"):
                FakeFastMCP.instance.tools["run_task"](
                    "continue", sessionId="first", session_id="second")
        server = FakeFastMCP.instance
        advertised = {tool.name: tool.inputSchema
                      for tool in asyncio.run(server.list_tools())}
        self.assertEqual(len(advertised["run_task"]["anyOf"]), 4)
        self.assertEqual(len(advertised["send_message"]["anyOf"]), 3)
        self.assertIn("chat", advertised["run_task"]["properties"]["role"]["description"])
        self.assertIn("session", advertised["send_message"]["properties"]["session_id"]["description"])
        self.assertEqual(server.name, "popot-agents")
        self.assertEqual(server.transport, "streamable-http")
        self.assertEqual(set(server.tools), {"run_task", "send_message", "list_chats", "get_chat"})
        self.assertTrue(server.settings["stateless_http"])
        from popot_agents.mcp_server import MCPHTTPLoggingMiddleware
        logged_app = server.streamable_http_app()
        self.assertIsInstance(logged_app, MCPHTTPLoggingMiddleware)
        self.assertEqual(logged_app.app, "upstream-asgi-app")


class HttpLoggingTests(unittest.TestCase):
    def test_mcp_logs_request_body_and_response_headers_without_changing_asgi_messages(self):
        from popot_agents.mcp_server import MCPHTTPLoggingMiddleware

        scope = {"type": "http", "method": "POST", "path": "/mcp",
                 "client": ("192.0.2.7", 4321), "headers": [
                     (b"content-type", b"application/json"),
                     (b"authorization", b"Bearer request-secret"),
                     (b"mcp-session-id", b"session-secret"),
                 ]}
        body = json.dumps({"method": "tools/call", "params": {
            "arguments": {"task": "Write a story", "api_key": "body-secret"}}}).encode()
        received = []
        sent = []

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            sent.append(message)

        async def app(_scope, receive_message, send_message):
            received.append(await receive_message())
            await send_message({"type": "http.response.start", "status": 200,
                                "headers": [(b"content-type", b"application/json"),
                                            (b"set-cookie", b"response-secret")]})
            await send_message({"type": "http.response.body", "body": b"{}"})

        output = io.StringIO()
        with redirect_stderr(output):
            asyncio.run(MCPHTTPLoggingMiddleware(app)(scope, receive, send))
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([event["event"] for event in events],
                         ["mcp_request", "mcp_request_body", "mcp_response"])
        self.assertEqual({event["request_id"] for event in events}, {events[0]["request_id"]})
        self.assertEqual(events[0]["client_ip"], "192.0.2.7")
        self.assertEqual(events[0]["method"], "POST")
        self.assertEqual(events[1]["body"]["params"]["arguments"]["task"], "Write a story")
        self.assertEqual(events[2]["status"], 200)
        self.assertIn(["content-type", "application/json"], events[2]["headers"])
        self.assertEqual(received[0]["body"], body)
        self.assertEqual(sent[1]["body"], b"{}")
        for secret in ("request-secret", "session-secret", "body-secret", "response-secret"):
            self.assertNotIn(secret, output.getvalue())


if __name__ == "__main__":
    unittest.main()

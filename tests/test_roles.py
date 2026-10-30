import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from popot_agents.tools import execute_tool
from popot_agents.worker.http_harness import run_http
from popot_agents.orchestrator.main import create_server, load_agents, load_roles
from popot_agents.runtime_config import RUNTIME


class FakeChat:
    def __init__(self, name):
        self.container_name = name
        self.closed = False
        self.restored = None

    def send(self, message):
        return {"answer": message}

    def is_alive(self):
        return not self.closed

    def restore(self, messages):
        self.restored = messages

    def close(self):
        self.closed = True


class FakeRunner:
    session_mode = "http"

    def __init__(self):
        self.tasks = []
        self.chats = []

    def __call__(self, task, role_config=None):
        self.tasks.append((task, role_config))
        return {"answer": task}

    def start_chat(self, chat_id, role_config=None):
        self.chats.append((chat_id, role_config, FakeChat(f"worker-{chat_id}")))
        return self.chats[-1][2]


class RoleDirectoryTests(unittest.TestCase):
    def test_role_names_come_from_filenames_and_allow_cross_file_delegation(self):
        configs = {
            'reviewer': {'agent': 'nous', 'instructions': 'Review code', 'tools': []},
            'chat': {'agent': 'nous', 'instructions': 'Delegate reviews', 'tools': [],
                     'allowed_roles': ['reviewer']},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for name, config in configs.items():
                (path / f'{name}.json').write_text(json.dumps(config))
            (path / 'README.md').write_text('Role settings')
            loaded = load_roles(path, {'nous': FakeRunner()})
            self.assertEqual(list(loaded), ['chat', 'reviewer'])
            for name, config in configs.items():
                self.assertEqual(loaded[name], {**config, 'name': name})
                self.assertEqual(json.loads((path / f'{name}.json').read_text()), config)

    def test_directory_rejects_invalid_filenames_and_role_settings(self):
        valid = {'agent': 'nous', 'instructions': 'Review code', 'tools': []}
        for filename, config, error in (
                ('Bad-role.json', valid, 'invalid role name'),
                ('reviewer.json', [], 'must define'),
                ('reviewer.json', {'reviewer': valid}, 'must define'),
                ('reviewer.json', {**valid, 'agent': 'missing'}, 'unknown agent'),
                ('reviewer.json', {**valid, 'allowed_roles': ['missing']}, 'allowed_roles')):
            with self.subTest(filename=filename, config=config), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                (path / filename).write_text(json.dumps(config))
                with self.assertRaisesRegex(ValueError, error):
                    load_roles(path, {'nous': FakeRunner()})

    def test_directory_reports_malformed_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'chat.json').write_text('{')
            with self.assertRaises(json.JSONDecodeError):
                load_roles(path, {'nous': FakeRunner()})


class RoleApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.runner = FakeRunner()
        self.roles = {
            "analyst": {"agent": "nous", "instructions": "Use precise arithmetic.", "tools": ["calculate"]},
            "chat": {"agent": "nous", "instructions": "Be concise.", "tools": []},
        }
        self.start_server()

    def start_server(self):
        self.server = create_server({"nous": self.runner}, port=0,
                                    session_dir=self.directory.name, roles=self.roles)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def request(self, method, path, payload=None):
        conn = HTTPConnection("127.0.0.1", self.server.server_port)
        conn.request(method, path, body=json.dumps(payload) if payload is not None else None,
                     headers={"Content-Type": "application/json"} if payload is not None else {})
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        conn.close()
        return result

    def test_legacy_role_file_derives_names_from_block_keys(self):
        path = Path(self.directory.name) / 'roles.json'
        config = {'agent': 'nous', 'instructions': 'Review code', 'tools': []}
        path.write_text(json.dumps({'reviewer': config}))
        loaded = load_roles(path, {'nous': self.runner})
        self.assertEqual(loaded['reviewer']['name'], 'reviewer')
        self.assertNotIn('name', json.loads(path.read_text())['reviewer'])

    def test_review_budget_fits_ax_and_leaves_room_for_final_answer(self):
        config = Path(__file__).resolve().parents[1] / 'config'
        roles = load_roles(config / 'roles', load_agents(config / 'agents.json'))
        from popot_agents.runtime_config import RUNTIME
        self.assertNotIn('timeout_seconds', roles['code_reviewer'])
        self.assertGreaterEqual(RUNTIME['timeouts']['turn_seconds'], 900)

    def test_role_selects_agent_and_tool_policy_for_task(self):
        status, result = self.request("POST", "/tasks", {"role": "analyst", "task": "2+3"})
        self.assertEqual(status, 200)
        self.assertEqual(result["role"], "analyst")
        self.assertEqual(result["agent"], "nous")
        self.assertEqual(result["answer"], "2+3")
        self.assertEqual(result["sessionId"], self.runner.chats[0][0])
        self.assertEqual(self.runner.chats[0][1], self.roles["analyst"])
        self.assertEqual(self.runner.tasks, [])
        self.assertEqual(self.request("POST", "/tasks", {"role": "missing", "task": "x"})[0], 404)
        self.assertEqual(self.request("POST", "/tasks", {"role": "analyst", "agent": "other", "task": "x"})[0], 409)

    def test_chat_restores_original_role_configuration_after_restart(self):
        status, first = self.request("POST", "/messages", {"role": "analyst", "message": "start"})
        self.assertEqual(status, 200)
        self.assertEqual(first["role"], "analyst")
        session_id = first["sessionId"]
        self.assertEqual(self.runner.chats[0][1], self.roles["analyst"])
        self.assertEqual(self.request("POST", "/messages", {
            "sessionId": session_id, "role": "chat", "message": "wrong role"})[0], 409)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.roles = {"analyst": {"agent": "nous", "instructions": "changed", "tools": []}}
        self.start_server()
        self.assertEqual(self.request("POST", "/messages", {
            "sessionId": session_id, "role": "chat", "message": "wrong role after restart",
        })[0], 409)
        status, reply = self.request("POST", "/messages", {
            "sessionId": session_id, "role": "analyst", "message": "continue",
        })
        self.assertEqual(status, 200)
        self.assertEqual(reply["role"], "analyst")
        self.assertEqual(self.runner.chats[1][1], {
            "agent": "nous", "instructions": "Use precise arithmetic.", "tools": ["calculate"]})
        self.assertEqual(self.runner.chats[1][2].restored, [
            {"role": "user", "content": "start"}, {"role": "assistant", "content": "start"}])
        self.assertEqual(self.request("GET", f"/chats/{session_id}")[1]["role"], "analyst")

    def test_load_roles_rejects_unknown_tools_and_cli_tool_policy(self):
        path = Path(self.directory.name) / "roles.json"
        path.write_text(json.dumps({"bad": {"agent": "nous", "instructions": "x", "tools": ["shell"]}}))
        with self.assertRaisesRegex(ValueError, "unknown tool"):
            load_roles(path, {"nous": self.runner})
        path.write_text(json.dumps({"bad": {"agent": "cli", "instructions": "x", "tools": ["calculate"]}}))
        cli = FakeRunner()
        cli.session_mode = "cli"
        with self.assertRaisesRegex(ValueError, "HTTP"):
            load_roles(path, {"cli": cli})


class FeatureTeamConfigTests(unittest.TestCase):
    def test_role_can_select_only_runtime_allowed_env_names(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "roles.json"
            role = {"agent": "nous", "instructions": "x", "tools": [],
                    "env_names": ["GITHUB_TOKEN"]}
            path.write_text(json.dumps({"chat": role}))
            with patch.dict(RUNTIME, {"role_env_names": ["GITHUB_TOKEN"]}):
                loaded = load_roles(path, {"nous": FakeRunner()})["chat"]
            self.assertEqual(loaded["env_names"], ["GITHUB_TOKEN"])
            with patch.dict(RUNTIME, {"role_env_names": []}):
                with self.assertRaisesRegex(ValueError, "env_names"):
                    load_roles(path, {"nous": FakeRunner()})

    def test_role_cron_validates_schedules_and_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "roles.json"
            role = {"agent": "nous", "instructions": "x", "tools": [],
                    "cron": [{"schedule": "0 9 * * 1-5", "task": "Daily report"}]}
            path.write_text(json.dumps({"analyst": role}))
            self.assertEqual(load_roles(path, {"nous": FakeRunner()})["analyst"]["cron"], role["cron"])
            for value in ([], [{"schedule": "bad", "task": "x"}],
                          [{"schedule": "* * * * *", "task": ""}],
                          [{"schedule": "* * * * *", "task": "x", "extra": True}]):
                with self.subTest(value=value):
                    role["cron"] = value
                    path.write_text(json.dumps({"analyst": role}))
                    with self.assertRaisesRegex(ValueError, "cron"):
                        load_roles(path, {"nous": FakeRunner()})

    def test_role_allowed_roles_accepts_other_roles_and_defaults_to_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "roles.json"
            roles = {
                "chat": {"agent": "nous", "instructions": "x", "tools": [],
                         "allowed_roles": ["analyst"]},
                "analyst": {"agent": "nous", "instructions": "x", "tools": []},
            }
            path.write_text(json.dumps(roles))
            loaded = load_roles(path, {"nous": FakeRunner()})
            self.assertEqual(loaded["chat"]["allowed_roles"], ["analyst"])
            self.assertEqual(loaded["analyst"].get("allowed_roles", []), [])
            roles["chat"]["allowed_roles"] = []
            path.write_text(json.dumps(roles))
            self.assertEqual(load_roles(path, {"nous": FakeRunner()})["chat"]["allowed_roles"], [])

    def test_role_allowed_roles_rejects_unknown_self_duplicate_and_invalid_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "roles.json"
            roles = {
                "chat": {"agent": "nous", "instructions": "x", "tools": []},
                "analyst": {"agent": "nous", "instructions": "x", "tools": []},
            }
            for value in (None, "analyst", ["missing"], ["chat"],
                          ["analyst", "analyst"], [1], [{}]):
                with self.subTest(value=value):
                    roles["chat"]["allowed_roles"] = value
                    path.write_text(json.dumps(roles))
                    with self.assertRaisesRegex(ValueError, "allowed_roles"):
                        load_roles(path, {"nous": FakeRunner()})

    def test_role_ttl_accepts_zero_and_rejects_invalid_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "roles.json"
            role = {"agent": "nous", "instructions": "x", "tools": [], "ttl_seconds": 0}
            runner = FakeRunner()
            path.write_text(json.dumps({"chat": role}))
            self.assertEqual(load_roles(path, {"nous": runner})["chat"]["ttl_seconds"], 0)
            for value in (-1, True, 1.5, "300"):
                role["ttl_seconds"] = value
                path.write_text(json.dumps({"chat": role}))
                with self.assertRaisesRegex(ValueError, "ttl_seconds"):
                    load_roles(path, {"nous": runner})

    def test_feature_team_roles_are_selectable_with_clear_responsibilities(self):
        config = Path(__file__).resolve().parents[1] / "config"
        roles = load_roles(config / "roles", load_agents(config / "agents.json"))
        expected = {
            "tech_lead", "backend_engineer", "frontend_engineer", "qa_engineer",
        }
        self.assertTrue(expected.issubset(roles))
        for removed in ("local_analyst", "product_manager", "product_designer", "data_analyst"):
            self.assertNotIn(removed, roles)
        for name in expected:
            self.assertGreater(len(roles[name]["instructions"]), 80, name)
        self.assertIn("calculate", roles["analyst"]["tools"])
        self.assertEqual(roles["backend_engineer"]["env_names"],
                         ["GITHUB_PERSONAL_ACCESS_TOKEN", "COGNEE_API_URL", "COGNEE_PASSWORD",
                          "GITGUARDIAN_API_KEY", "SONAR_TOKEN"])

    def test_frontend_and_qa_can_execute_checks_with_readonly_github_access(self):
        config = Path(__file__).resolve().parents[1] / 'config'
        roles = load_roles(config / 'roles', load_agents(config / 'agents.json'))
        for name in ('frontend_engineer', 'qa_engineer'):
            role = roles[name]
            self.assertLessEqual({'bash', 'read_file', 'write_file', 'git_clone'}, set(role['tools']))
            self.assertTrue(role['permissions']['shell'])
            self.assertEqual(role['permissions']['workspace'], 'persistent')
            self.assertIn('GITHUB_PERSONAL_ACCESS_TOKEN', role['env_names'])
            self.assertLessEqual({'GITGUARDIAN_API_KEY', 'SONAR_TOKEN'}, set(role['env_names']))
            github = role['mcpServers']['github']
            self.assertTrue(github['url'].endswith('/readonly'))
            self.assertIn('get_file_contents', github['tools'])
            self.assertFalse({'create_pull_request', 'create_or_update_file'} & set(github['tools']))

    def test_demo_cron_role_has_daily_utc_time_task(self):
        config = Path(__file__).resolve().parents[1] / "config"
        roles = load_roles(config / "roles", load_agents(config / "agents.json"))
        demo = roles["cron_demo"]
        self.assertEqual(demo["agent"], "omniroute")
        self.assertEqual(demo["tools"], ["utc_time"])
        self.assertEqual(demo["cron"], [{"schedule": "0 9 * * *",
                                        "task": "Use utc_time and report the current UTC date and time."}])


class ToolLoopTests(unittest.TestCase):
    def test_only_configured_safe_tools_can_run(self):
        self.assertEqual(execute_tool("calculate", {"expression": "2+3*4"}, ["calculate"]), "14")
        with self.assertRaisesRegex(ValueError, "not allowed"):
            execute_tool("utc_time", {}, ["calculate"])
        with self.assertRaisesRegex(ValueError, "unsupported expression"):
            execute_tool("calculate", {"expression": "__import__('os').system('id')"}, ["calculate"])

    def test_http_harness_executes_allowed_tool_then_returns_final_answer(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append(payload)
                if len(received) == 1:
                    message = {"content": None, "tool_calls": [{
                        "id": "call-1", "type": "function", "function": {
                            "name": "calculate", "arguments": '{"expression":"2+3*4"}'}}]}
                else:
                    message = {"content": "14"}
                body = json.dumps({"choices": [{"message": message}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(os.environ, {"HARNESS_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
                                      "HARNESS_MODEL": "test"}):
                answer = run_http("compute", {"instructions": "Be exact.", "tools": ["calculate"]})
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual(answer, "14")
        self.assertEqual(received[0]["messages"][0], {"role": "system", "content": "Be exact."})
        self.assertEqual(received[0]["tools"][0]["function"]["name"], "calculate")
        self.assertEqual(received[1]["messages"][-1], {
            "role": "tool", "tool_call_id": "call-1", "content": "14"})


if __name__ == "__main__":
    unittest.main()

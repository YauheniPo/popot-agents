import os
import builtins
import json
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from popot_agents import tools as harness_tools
from popot_agents.tools import execute_tool
from popot_agents.orchestrator.main import DockerAgentRunner, load_agents, load_roles
from popot_agents.runtime_config import RUNTIME


BACKEND_TOOLS = [
    "bash", "read_file", "write_file", "git_clone", "download_file",
]


class BackendRoleTests(unittest.TestCase):
    def test_role_env_is_forwarded_to_tools_without_other_credentials(self):
        with patch.dict(os.environ, {"HARNESS_TOOL_ENV_NAMES_JSON": '["GITHUB_TOKEN"]',
                 "GITHUB_TOKEN": "selected-secret",
                 "NOUS_API_KEY": "model-secret", "AX_DELEGATION_TOKEN": "runtime-secret"}):
            forwarded = harness_tools.safe_tool_env()
        self.assertEqual(forwarded["GITHUB_TOKEN"], "selected-secret")
        self.assertNotIn("NOUS_API_KEY", forwarded)
        self.assertNotIn("AX_DELEGATION_TOKEN", forwarded)
        self.assertIn("HOME", forwarded)
        with patch.dict(os.environ, {"HARNESS_TOOL_ENV_NAMES_JSON": "[]",
                                     "GITHUB_TOKEN": "selected-secret"}):
            self.assertNotIn("GITHUB_TOKEN", harness_tools.safe_tool_env())

    @patch("popot_agents.orchestrator.main.subprocess.run")
    def test_docker_role_adds_only_selected_env_names(self, run):
        run.return_value = subprocess.CompletedProcess(["docker", "run"], 0, '{"answer":"ok"}', "")
        runner = DockerAgentRunner(image="example:local", network="bridge",
                                   env_names=["OPENROUTER_API_KEY"])
        with patch.dict(RUNTIME, {"role_env_names": ["GITHUB_TOKEN"]}), \
             patch.dict(os.environ, {"OPENROUTER_API_KEY": "model-secret",
                                  "GITHUB_TOKEN": "selected-secret", "OTHER_TOKEN": "other-secret"}):
            runner("task", {"agent": "openrouter", "instructions": "x", "tools": [],
                            "env_names": ["GITHUB_TOKEN"]})
        args = run.call_args.args[0]
        self.assertIn("OPENROUTER_API_KEY", args)
        self.assertIn("GITHUB_TOKEN", args)
        self.assertNotIn("OTHER_TOKEN", args)
        self.assertNotIn("selected-secret", args)
        self.assertIn('HARNESS_TOOL_ENV_NAMES_JSON=["GITHUB_TOKEN"]', args)

    def test_mcp_launcher_resolves_command_before_dropping_privileges(self):
        launcher = Path(harness_tools.__file__).parent / "worker" / "tool_launcher.py"
        source = compile(launcher.read_text(), str(launcher), "exec")
        real_import = builtins.__import__
        dropped = False

        def drop_uid(uid):
            nonlocal dropped
            self.assertEqual(uid, 10001)
            dropped = True

        def guarded_import(name, *args, **kwargs):
            if dropped:
                raise ModuleNotFoundError(f"No module named '{name}' after UID drop")
            return real_import(name, *args, **kwargs)

        def exec_as_tool_user(path, arguments, environment):
            self.assertTrue(dropped)
            self.assertEqual(path, str(executable))
            self.assertEqual(arguments, ["test-mcp", "--stdio"])
            self.assertEqual(environment["PATH"], directory)

        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "test-mcp"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
            with patch.dict(os.environ, {"PATH": directory}), \
                 patch.object(sys, "argv", [str(launcher), "test-mcp", "--stdio"]), \
                 patch.object(os, "geteuid", return_value=0), \
                 patch.object(os, "setgroups") as groups, \
                 patch.object(os, "setgid") as gid, \
                 patch.object(os, "setuid", side_effect=drop_uid), \
                 patch.object(os, "execve", side_effect=exec_as_tool_user) as execute, \
                 patch.object(builtins, "__import__", side_effect=guarded_import):
                exec(source, {"__name__": "__main__"})
            groups.assert_called_once_with([])
            gid.assert_called_once_with(10001)
            execute.assert_called_once()

    def test_mcp_launcher_works_outside_package_directory(self):
        from popot_agents.worker.mcp_client import _parameters
        fake_sdk = SimpleNamespace(StdioServerParameters=lambda **kwargs: SimpleNamespace(**kwargs))
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(harness_tools, "WORKSPACE_ROOT", Path(directory)), \
             patch.dict(sys.modules, {"mcp": fake_sdk}), \
             patch.dict(os.environ, {"AX_DELEGATION_TOKEN": "private-test-value"}):
            params = _parameters({"command": [sys.executable, "-c",
                'import os; print(os.getenv("AX_DELEGATION_TOKEN", "unset"))']})
            self.assertNotIn("PYTHONPATH", params.env)
            completed = subprocess.run([params.command, *params.args], cwd=directory,
                                       env=params.env, capture_output=True, text=True, timeout=5)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "unset")

    def test_file_helpers_work_from_ax_workspace_without_pythonpath(self):
        real_run = subprocess.run
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(harness_tools, "WORKSPACE_ROOT", Path(directory)), \
             patch("popot_agents.tools.subprocess.run",
                   side_effect=lambda *args, **kwargs: real_run(*args, cwd=directory, **kwargs)):
            self.assertEqual(execute_tool("write_file", {
                "path": "code.txt", "content": "workspace data"}, BACKEND_TOOLS), "wrote code.txt")
            self.assertEqual(execute_tool("read_file", {"path": "code.txt"}, BACKEND_TOOLS),
                             "workspace data")

    def test_backend_role_has_explicit_permissions_and_mcp_tools(self):
        root = Path(__file__).resolve().parents[1] / "config"
        roles = load_roles(root / "roles.json", load_agents(root / "agents.json"))
        backend = roles["backend_engineer"]
        self.assertTrue(set(BACKEND_TOOLS).issubset(backend["tools"]))
        self.assertEqual(backend["permissions"], {
            "workspace": "persistent", "shell": True, "internet": True,
        })
        self.assertEqual(set(backend["mcpServers"]), {"fetch", "git"})
        self.assertEqual(backend["mcpServers"]["fetch"]["tools"], ["fetch"])
        self.assertEqual(backend["mcpServers"]["git"]["tools"],
                         ["git_status", "git_diff_unstaged"])
        self.assertEqual(backend["max_tool_rounds"], 12)

    def test_backend_chat_gets_a_durable_private_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            role = {"permissions": {"workspace": "persistent", "shell": True,
                                    "internet": True}, "tools": BACKEND_TOOLS}
            with patch.dict(os.environ, {"AGENT_WORKSPACE_DIR": directory}):
                args = DockerAgentRunner(network="bridge")._docker_command(
                    "popot-chat-1234", detached=True, role_config=role)
            workspace = Path(directory).resolve() / "popot-chat-1234"
            self.assertTrue(workspace.is_dir())
            self.assertIn(f"type=bind,src={workspace},dst=/workspace", args)
            self.assertNotIn("/workspace:rw,uid=10001,gid=10001,size=256m", args)
            self.assertIn("--user", args)
            self.assertIn("0:10001", args)
            self.assertIn("HARNESS_SOCKET_PATH=/run/chat.sock", args)

    def test_existing_persistent_workspace_is_writable_by_tool_group(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "popot-chat-1234"
            workspace.mkdir(mode=0o700)
            role = {"permissions": {"workspace": "persistent"}}
            with patch.dict(os.environ, {"AGENT_WORKSPACE_DIR": directory}), \
                 patch("popot_agents.orchestrator.main.os.geteuid", return_value=0), \
                 patch("popot_agents.orchestrator.main.os.chown") as chown:
                DockerAgentRunner.workspace_path("popot-chat-1234", role, create=True)
            chown.assert_called_once_with(workspace.resolve(), -1, 10001)
            self.assertEqual(workspace.stat().st_mode & 0o777, 0o770)

    def test_file_and_shell_tools_work_only_with_allowlist_and_hide_model_key(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(harness_tools, "WORKSPACE_ROOT", Path(directory)):
                self.assertEqual(execute_tool("write_file", {
                    "path": "app/main.py", "content": "print('ok')\n"}, BACKEND_TOOLS),
                    "wrote app/main.py")
                self.assertEqual(execute_tool("read_file", {"path": "app/main.py"},
                                              BACKEND_TOOLS), "print('ok')\n")
                with patch.dict(os.environ, {"NOUS_API_KEY": "private-test-value"}):
                    output = execute_tool("bash", {
                        "command": "printf '%s' \"${NOUS_API_KEY:-unset}\""}, BACKEND_TOOLS)
                self.assertIn("unset", output)
                self.assertNotIn("private-test-value", output)
                with self.assertRaisesRegex(ValueError, "outside workspace"):
                    execute_tool("write_file", {"path": "../escape", "content": "x"},
                                 BACKEND_TOOLS)
                with self.assertRaisesRegex(ValueError, "not allowed"):
                    execute_tool("bash", {"command": "pwd"}, ["calculate"])

    def test_bash_runs_as_unprivileged_user_while_model_process_keeps_credential(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(harness_tools, "WORKSPACE_ROOT", Path(directory)):
                with patch("popot_agents.tools.os.geteuid", return_value=0):
                    with patch("popot_agents.tools.subprocess.run") as run:
                        run.return_value.returncode = 0
                        execute_tool("bash", {"command": "pwd"}, ["bash"])
                self.assertEqual(run.call_args.kwargs["user"], 10001)
                self.assertEqual(run.call_args.kwargs["group"], 10001)

    def test_file_write_also_runs_as_unprivileged_workspace_user(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(harness_tools, "WORKSPACE_ROOT", Path(directory)):
                with patch("popot_agents.tools.os.geteuid", return_value=0):
                    with patch("popot_agents.tools.subprocess.run") as run:
                        run.return_value.returncode = 0
                        run.return_value.stdout = "wrote code.py\n"
                        execute_tool("write_file", {"path": "code.py", "content": "x"},
                                     ["write_file"])
                self.assertEqual(run.call_args.kwargs["user"], 10001)
                self.assertEqual(run.call_args.kwargs["group"], 10001)

    def test_git_clone_does_not_accept_local_file_url(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(harness_tools, "WORKSPACE_ROOT", Path(directory)):
                with self.assertRaisesRegex(ValueError, "HTTPS"):
                    execute_tool("git_clone", {"url": "file:///etc", "directory": "repo"},
                                 BACKEND_TOOLS)

    def test_shell_and_mcp_require_internet_permission(self):
        root = Path(__file__).resolve().parents[1] / "config"
        roles = load_roles(root / "roles.json", load_agents(root / "agents.json"))
        roles["backend_engineer"]["permissions"]["internet"] = False
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "roles.json"
            roles["backend_engineer"]["tools"] = ["bash"]
            roles["backend_engineer"]["mcpServers"] = {}
            path.write_text(json.dumps(roles), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "internet permission"):
                load_roles(path, load_agents(root / "agents.json"))
            roles["backend_engineer"]["tools"] = []
            roles["backend_engineer"]["mcpServers"] = {
                "fetch": {"command": ["python", "-m", "mcp_server_fetch"], "tools": ["fetch"]}}
            path.write_text(json.dumps(roles), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "internet permission"):
                load_roles(path, load_agents(root / "agents.json"))

    def test_http_harness_exposes_and_calls_only_role_selected_mcp_tools(self):
        from popot_agents.worker.http_harness import run_http
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import json
        import threading

        calls = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append(payload)
                message = ({"content": None, "tool_calls": [{"id": "call-1", "type": "function",
                            "function": {"name": "mcp__fetch__fetch",
                                         "arguments": '{"url":"https://example.com"}'}}]}
                           if len(calls) == 1 else {"content": "done"})
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
        config = {"fetch": {"command": ["python", "-m", "mcp_server_fetch"],
                            "tools": ["fetch"]}}
        discovered = {"mcp__fetch__fetch": {"server": "fetch", "native_name": "fetch",
                      "schema": {"type": "function", "function": {"name": "mcp__fetch__fetch",
                       "description": "fetch", "parameters": {"type": "object"}}}}}
        try:
            with patch.dict(os.environ, {"HARNESS_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
                                      "HARNESS_MODEL": "test"}):
                with patch("popot_agents.worker.mcp_client.discover_tools", return_value=discovered), \
                     patch("popot_agents.worker.mcp_client.call_tool", return_value="page text") as call:
                    answer = run_http("fetch", {"tools": [], "mcpServers": config})
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual(answer, "done")
        self.assertEqual(calls[0]["tools"], [discovered["mcp__fetch__fetch"]["schema"]])
        self.assertEqual(calls[1]["messages"][-1]["content"], "page text")
        self.assertEqual(call.call_args.args, (config["fetch"], "fetch", {"url": "https://example.com"}))


if __name__ == "__main__":
    unittest.main()

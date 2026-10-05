"""Delegation must enforce API-side authority and clean up child actors."""

import io
import json
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from email.message import Message
from types import SimpleNamespace
from unittest.mock import patch

from ax_local.api.delegation import A2AError, DelegationService, start_a2a_server
from popot_agents.orchestrator.main import create_server
from popot_agents.orchestrator.session_store import SessionStore


class FakeChat:
    def __init__(self, runner, session_id, config):
        self.runner, self.session_id, self.config = runner, session_id, config
        self.closed = False

    def send(self, task, *, timeout_seconds=None):
        with self.runner.delegation.turn(self.session_id, timeout_seconds):
            self.runner.entered.set()
            if self.runner.callback:
                self.runner.callback(self, task)
            return {"answer": "child result: " + task}

    def close(self):
        self.closed = True
        self.runner.delegation.revoke(self.session_id)


class FakeRunner:
    def __init__(self):
        self.chats = []
        self.entered = threading.Event()
        self.callback = None
        self.recovered = []

    def close_session(self, session_id):
        self.recovered.append(session_id)

    def start_chat(self, session_id, config, *, deadline=None, cancel_event=None):
        self.delegation.worker_environment(session_id, config)
        chat = FakeChat(self, session_id, config)
        self.chats.append(chat)
        return chat


class DelegationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runner = FakeRunner()
        self.roles = {name: {"agent": "test", "instructions": name, "tools": [],
                             "_ax_role": name}
                      for name in ("tech_lead", "backend_engineer", "qa_engineer")}
        self.roles["tech_lead"]["allowed_roles"] = ["backend_engineer"]
        self.roles["backend_engineer"]["allowed_roles"] = ["qa_engineer", "tech_lead"]
        self.policy = {"enabled": True, "port": 8005, "max_depth": 2,
                       "max_calls_per_turn": 4, "max_concurrent_tasks": 2,
                       "turn_timeout_seconds": 300, "poll_interval_seconds": 0.01}
        self.store = SessionStore(self.temporary.name)
        self.service = DelegationService(self.roles, self.policy, self.runner, self.store,
                                         "http://127.0.0.1:8005")
        self.runner.delegation = self.service
        self.addCleanup(self.service.close)
        self.parent = "0123456789abcdef"
        env = self.service.worker_environment(self.parent, self.roles["tech_lead"])
        self.token = env["AX_DELEGATION_TOKEN"]

    def send(self, token=None, role="backend_engineer", message_id="message-1", task="implement"):
        return self.service.dispatch(token or self.token, role, "SendMessage", {
            "message": {"messageId": message_id, "role": "ROLE_USER",
                        "parts": [{"text": task}]},
            "configuration": {"blocking": False},
        })["task"]

    def wait_task(self, task_id, token=None, role="backend_engineer"):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            task = self.service.dispatch(token or self.token, role, "GetTask", {"id": task_id})
            if task["status"]["state"] not in {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}:
                return task
            time.sleep(0.01)
        self.fail("child did not finish")

    def test_delegates_and_saves_child_transcript_and_parent_link(self):
        with self.service.turn(self.parent, 5):
            task = self.wait_task(self.send()["id"])
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(task["artifacts"][0]["parts"][0]["text"], "child result: implement")
        saved = self.store.get(task["id"])
        self.assertEqual(saved["role"], "backend_engineer")
        self.assertEqual(saved["roleConfig"]["_ax_delegation"]["parent_session_id"], self.parent)
        self.assertEqual(saved["messages"][-1]["content"], "child result: implement")
        self.assertTrue(self.runner.chats[0].closed)
        self.assertNotIn(self.token, str(saved))

    def test_denies_unavailable_role_even_if_request_claims_other_parent(self):
        with self.service.turn(self.parent, 5), self.assertRaisesRegex(A2AError, "not allowed"):
            self.send(role="qa_engineer")
        self.assertEqual(self.runner.chats, [])

    def test_capability_is_invalid_outside_parent_turn_or_after_revoke(self):
        with self.assertRaises(A2AError):
            self.send()
        self.service.revoke(self.parent)
        with self.assertRaises(A2AError):
            self.send()

    def test_repeated_message_id_does_not_spawn_another_child(self):
        with self.service.turn(self.parent, 5):
            first = self.wait_task(self.send()["id"])
            second = self.send()
            self.assertEqual(first["id"], second["id"])
            with self.assertRaisesRegex(A2AError, "different"):
                self.send(task="changed input")
        self.assertEqual(len(self.runner.chats), 1)

    def test_nested_delegation_and_cycle_rejection(self):
        nested = []
        def callback(chat, _task):
            if chat.config["_ax_role"] == "backend_engineer":
                token = self.service.worker_environment(chat.session_id, chat.config)["AX_DELEGATION_TOKEN"]
                with self.assertRaisesRegex(A2AError, "cycle"):
                    self.send(token=token, role="tech_lead")
                nested.append(self.wait_task(self.send(token=token, role="qa_engineer")["id"],
                                             token=token, role="qa_engineer"))
        self.runner.callback = callback
        with self.service.turn(self.parent, 5):
            self.wait_task(self.send()["id"])
        self.assertEqual(nested[0]["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertTrue(all(chat.closed for chat in self.runner.chats))

    def test_child_capacity_rejects_without_queueing(self):
        release = threading.Event()
        self.runner.callback = lambda *_: release.wait(1)
        try:
            with self.service.turn(self.parent, 5):
                one = self.send(message_id="one")
                two = self.send(message_id="two")
                with self.assertRaisesRegex(A2AError, "busy"):
                    self.send(message_id="three")
                release.set()
                self.wait_task(one["id"])
                self.wait_task(two["id"])
        finally:
            release.set()

    def test_failures_are_persisted_and_do_not_leak_exception_secrets(self):
        self.runner.callback = lambda *_: (_ for _ in ()).throw(RuntimeError("private-key"))
        with self.service.turn(self.parent, 5):
            task = self.wait_task(self.send()["id"])
        self.assertEqual(task["status"]["state"], "TASK_STATE_FAILED")
        self.assertNotIn("private-key", str(task))
        self.assertEqual(task["metadata"]["failure"], {
            "stage": "worker_message", "code": "worker_failed", "error_type": "RuntimeError"})
        self.assertTrue(self.runner.chats[0].closed)

    def test_worker_model_and_mcp_failures_have_safe_diagnostics(self):
        from popot_agents.orchestrator.main import AgentRunError
        failures = [
            ("model endpoint returned HTTP 429", "model_http_error",
             "model endpoint returned HTTP 429"),
            ("MCP server fetch failed during discovery: private-key", "mcp_discovery_failed",
             "MCP server fetch failed during discovery"),
        ]
        for index, (error, code, reason) in enumerate(failures):
            with self.subTest(code=code):
                self.runner.callback = lambda *_: (_ for _ in ()).throw(AgentRunError(error))
                with self.service.turn(self.parent, 5):
                    task = self.wait_task(self.send(message_id=f"failure-{index}")["id"])
                self.assertEqual(task["metadata"]["failure"]["code"], code)
                self.assertEqual(task["status"]["message"]["parts"][0]["text"], reason)
                self.assertNotIn("private-key", str(task))

    def test_child_startup_failure_is_distinct_from_child_model_failure(self):
        import subprocess
        with self.service.turn(self.parent, 5), \
             patch.object(self.runner, "start_chat", side_effect=subprocess.TimeoutExpired("ax", 10)):
            task = self.wait_task(self.send()["id"])
        self.assertEqual(task["metadata"]["failure"], {
            "stage": "worker_startup", "code": "ax_command_timeout", "error_type": "TimeoutExpired"})
        self.assertEqual(self.runner.chats, [])

    def test_unknown_task_and_other_parent_cannot_read_child(self):
        env = self.service.worker_environment("1111111111111111", self.roles["tech_lead"])
        with self.service.turn(self.parent, 5), self.service.turn("1111111111111111", 5):
            task = self.wait_task(self.send()["id"])
            with self.assertRaises(A2AError):
                self.service.dispatch(env["AX_DELEGATION_TOKEN"], "backend_engineer", "GetTask",
                                      {"id": task["id"]})

    def test_depth_and_total_call_limits(self):
        self.policy["max_calls_per_turn"] = 1
        with self.service.turn(self.parent, 5):
            self.wait_task(self.send()["id"])
            with self.assertRaisesRegex(A2AError, "call limit"):
                self.send(message_id="second")
        self.policy["max_depth"] = 1
        failures = []
        def callback(chat, _):
            token = self.service.worker_environment(chat.session_id, chat.config)["AX_DELEGATION_TOKEN"]
            with self.assertRaisesRegex(A2AError, "depth limit"):
                self.send(token=token, role="qa_engineer")
            failures.append("denied")
        self.runner.callback = callback
        self.policy["max_calls_per_turn"] = 4
        with self.service.turn(self.parent, 5):
            self.wait_task(self.send(message_id="new-turn")["id"])
        self.assertEqual(failures, ["denied"])

    def test_cancel_and_parent_completion_stop_children(self):
        release = threading.Event()
        self.runner.callback = lambda *_: release.wait(1)
        try:
            with self.service.turn(self.parent, 5):
                task = self.send()
                self.assertTrue(self.runner.entered.wait(1))
                canceled = self.service.dispatch(self.token, "backend_engineer", "CancelTask", {"id": task["id"]})
                self.assertEqual(canceled["status"]["state"], "TASK_STATE_CANCELED")
                self.assertTrue(self.runner.chats[0].closed)
                release.set()
            for job in list(self.service.jobs.values()):
                job.thread.join(1)
            self.assertEqual(self.store.get(task["id"])["roleConfig"]["_ax_delegation"]["state"],
                             "TASK_STATE_CANCELED")
        finally:
            release.set()

    def test_active_child_cannot_be_deleted_until_owner_finishes(self):
        release = threading.Event()
        self.runner.callback = lambda *_: release.wait(2)
        with patch("popot_agents.orchestrator.main.ChatServer") as server_class:
            create_server({"test": self.runner})
        handler_class = server_class.call_args.args[1]
        server = SimpleNamespace(store=self.store, live={}, live_lock=threading.RLock(),
                                 session_lock=lambda _session_id: threading.RLock(),
                                 delegation=self.service, cron=None)

        def delete(session_id):
            handler = handler_class.__new__(handler_class)
            handler.client_address = ("192.0.2.7", 1234)
            handler.command = "DELETE"
            handler.path = f"/chats/{session_id}"
            handler.request_version = "HTTP/1.1"
            handler.requestline = f"DELETE {handler.path} HTTP/1.1"
            handler.headers = Message()
            handler.server = server
            handler.wfile = io.BytesIO()
            with redirect_stderr(io.StringIO()):
                handler.do_DELETE()
            raw = handler.wfile.getvalue()
            return int(raw.split(b" ", 2)[1]), json.loads(raw.split(b"\r\n\r\n", 1)[1])

        try:
            with self.service.turn(self.parent, 5):
                session_id = self.send()["id"]
                self.assertTrue(self.runner.entered.wait(2))
                self.assertEqual(delete(session_id)[0], 409)
                self.assertIsNotNone(self.store.get(session_id))
                release.set()
                self.wait_task(session_id)
                self.assertEqual(self.store.get(session_id)["roleConfig"]["_ax_delegation"]["state"],
                                 "TASK_STATE_COMPLETED")
                self.assertEqual(delete(session_id), (200, {"status": "closed"}))
                self.assertIsNone(self.store.get(session_id))
        finally:
            release.set()

    def test_restart_marks_pending_tasks_failed_and_preserves_idempotency(self):
        with self.service.turn(self.parent, 5):
            task = self.wait_task(self.send()["id"])
        saved = self.store.get(task["id"])
        saved["roleConfig"]["_ax_delegation"]["state"] = "TASK_STATE_WORKING"
        self.store.save(saved["sessionId"], saved["agent"], saved["messages"], saved["role"], saved["roleConfig"])
        self.service.recover_interrupted()
        self.assertEqual(self.runner.recovered, [task["id"]])
        with self.service.turn(self.parent, 5):
            replay = self.send()
        self.assertEqual(replay["id"], task["id"])
        self.assertEqual(replay["status"]["state"], "TASK_STATE_FAILED")
        self.assertEqual(len(self.runner.chats), 1)

    def test_parent_turn_end_cancels_a_running_child(self):
        release = threading.Event()
        self.runner.callback = lambda *_: release.wait(1)
        try:
            with self.service.turn(self.parent, 5):
                task = self.send()
                self.assertTrue(self.runner.entered.wait(1))
            self.assertTrue(self.runner.chats[0].closed)
            release.set()
            for job in list(self.service.jobs.values()):
                job.thread.join(1)
            self.assertEqual(self.store.get(task["id"])["roleConfig"]["_ax_delegation"]["state"],
                             "TASK_STATE_CANCELED")
        finally:
            release.set()

    def test_child_completion_cancels_unfinished_grandchild(self):
        release = threading.Event()
        grandchild_started = threading.Event()
        nested = []
        def callback(chat, _):
            if chat.config["_ax_role"] == "backend_engineer":
                token = self.service.worker_environment(chat.session_id, chat.config)["AX_DELEGATION_TOKEN"]
                nested.append(self.send(token=token, role="qa_engineer")["id"])
                grandchild_started.wait(1)
            else:
                grandchild_started.set()
                release.wait(1)
        self.runner.callback = callback
        try:
            with self.service.turn(self.parent, 5):
                self.wait_task(self.send()["id"])
                release.set()
                for job in list(self.service.jobs.values()):
                    job.thread.join(1)
            saved = self.store.get(nested[0])
            self.assertEqual(saved["roleConfig"]["_ax_delegation"]["state"], "TASK_STATE_CANCELED")
            self.assertTrue(all(chat.closed for chat in self.runner.chats))
        finally:
            release.set()

    def test_existing_parent_sessions_gain_delegation_when_recreated(self):
        self.store.save(self.parent, "test", [], "tech_lead", {"tools": []})
        config = self.service.prepare_config(self.parent, {"tools": []})
        self.assertEqual(config["_ax_role"], "tech_lead")
        self.assertEqual(config["timeout_seconds"], 300)

    def test_saved_role_settings_cannot_expand_current_delegation_permissions(self):
        parent = "1111111111111111"
        stale = {**self.roles["tech_lead"], "allowed_roles": ["qa_engineer"]}
        env = self.service.worker_environment(parent, stale)
        import json
        self.assertEqual(json.loads(env["AX_DELEGATION_ROLES"]), ["backend_engineer"])
        with self.service.turn(parent, 5), self.assertRaisesRegex(A2AError, "not allowed"):
            self.send(token=env["AX_DELEGATION_TOKEN"], role="qa_engineer")

    def test_actual_worker_tool_waits_for_a2a_child_result(self):
        from ax_local.worker.delegation import make_tools
        server = start_a2a_server(self.service, "127.0.0.1", 0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        tools = make_tools({"AX_DELEGATION_URL": f"http://127.0.0.1:{server.server_port}",
                            "AX_DELEGATION_TOKEN": self.token,
                            "AX_DELEGATION_ROLES": '["backend_engineer"]',
                            "AX_DELEGATION_POLL_SECONDS": "0.01"})
        import json
        with self.service.turn(self.parent, 5):
            output = tools["delegate_task"]["call"]({"role": "backend_engineer", "task": "actual HTTP"},
                                                    timeout_seconds=2, call_id="call-1")
        result = json.loads(output)
        self.assertEqual(result["answer"], "child result: actual HTTP")
        self.assertEqual(result["state"], "TASK_STATE_COMPLETED")

    def test_worker_retries_lost_submission_response_without_duplicate_actor(self):
        import json
        from urllib import request, error
        from ax_local.worker.delegation import make_tools
        server = start_a2a_server(self.service, "127.0.0.1", 0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        tools = make_tools({"AX_DELEGATION_URL": f"http://127.0.0.1:{server.server_port}",
                            "AX_DELEGATION_TOKEN": self.token,
                            "AX_DELEGATION_ROLES": '["backend_engineer"]',
                            "AX_DELEGATION_POLL_SECONDS": "0.01"})
        original = request.urlopen
        messages = []
        def drop_response(call, **kwargs):
            response = original(call, **kwargs)
            body = json.loads(call.data)
            if body["method"] == "SendMessage":
                messages.append(body["params"]["message"]["messageId"])
                if len(messages) == 1:
                    response.read()
                    response.close()
                    raise error.URLError("lost response")
            return response
        with self.service.turn(self.parent, 5), \
             patch("ax_local.worker.delegation.request.urlopen", side_effect=drop_response):
            result = json.loads(tools["delegate_task"]["call"](
                {"role": "backend_engineer", "task": "retry"}, timeout_seconds=2, call_id="tool-call"))
        self.assertEqual(result["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(messages, [messages[0], messages[0]])
        self.assertEqual(len(self.runner.chats), 1)

    def test_agent_card_and_jsonrpc_over_http(self):
        import json
        from urllib import request
        server = start_a2a_server(self.service, "127.0.0.1", 0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/a2a/backend_engineer"
        with request.urlopen(url + "/.well-known/agent-card.json") as response:
            card = json.load(response)
        self.assertEqual(card["supportedInterfaces"][0]["protocolBinding"], "JSONRPC")
        with self.service.turn(self.parent, 5):
            body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {
                "message": {"messageId": "http-message", "role": "ROLE_USER",
                            "parts": [{"text": "HTTP task"}]}}}
            call = request.Request(url, data=json.dumps(body).encode(), headers={
                "Content-Type": "application/json", "A2A-Version": "1.0",
                "Authorization": "Bearer " + self.token})
            with request.urlopen(call) as response:
                result = json.load(response)
            self.assertEqual(result["id"], 1)
            self.wait_task(result["result"]["task"]["id"])


if __name__ == "__main__":
    unittest.main()

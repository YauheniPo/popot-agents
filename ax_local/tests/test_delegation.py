"""Delegation must enforce API-side authority and clean up child actors."""

import asyncio
import io
import json
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from email.message import Message
from types import SimpleNamespace
from unittest.mock import patch

from ax_local.api.delegation import A2AError, DelegationService, start_a2a_server
from ax_local.api.ax_runner import AxAgentRunner, AxChat
from ax_local.tests.test_worker_queue import wait_for
from popot_agents.orchestrator.main import create_server
from popot_agents.orchestrator.session_store import SessionStore
from popot_agents.progress import emit_progress, observe_progress


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
                       "max_calls_per_turn": 4,
                       "turn_timeout_seconds": 300, "poll_interval_seconds": 0.01}
        self.store = SessionStore(self.temporary.name)
        self.service = DelegationService(self.roles, self.policy, {"test": self.runner}, self.store,
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
            "configuration": {"returnImmediately": True},
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

    def test_child_uses_its_role_profile_instead_of_the_parent_provider(self):
        child_runner = FakeRunner()
        child_runner.delegation = self.service
        self.service.runners['child_provider'] = child_runner
        self.roles['backend_engineer']['agent'] = 'child_provider'
        with self.service.turn(self.parent, 5):
            task = self.wait_task(self.send()['id'])
        self.assertEqual(task['status']['state'], 'TASK_STATE_COMPLETED')
        self.assertEqual(self.runner.chats, [])
        self.assertEqual(len(child_runner.chats), 1)
        self.assertTrue(child_runner.chats[0].closed)
        self.assertEqual(self.store.get(task['id'])['agent'], 'child_provider')

    def test_nested_child_progress_reaches_parent_before_final_answer_via_a2a(self):
        from ax_local.worker.delegation import make_tools
        received, completed = [], threading.Event()
        server = start_a2a_server(self.service, '127.0.0.1', 0)
        self.addCleanup(server.server_close)

        def delegate(session_id, config, role):
            env = self.service.worker_environment(session_id, config)
            env['AX_DELEGATION_POLL_SECONDS'] = '0.01'
            tools = make_tools(env)
            return json.loads(tools['delegate_task']['call'](
                {'role': role, 'task': 'inspect'}, timeout_seconds=5, call_id=session_id + '-inspect'))

        def notify(message):
            received.append(message)
            if json.loads(message).get('agent_role') == 'qa_engineer' and json.loads(message).get('name') == 'mcp__github__pull_request_read':
                completed.set()

        def callback(chat, _):
            emit_progress('model_request')
            if chat.config['_ax_role'] == 'backend_engineer':
                result = delegate(chat.session_id, chat.config, 'qa_engineer')
                self.assertEqual(result.get('state'), 'TASK_STATE_COMPLETED', result)
            else:
                emit_progress('tool_call', name='mcp__github__pull_request_read',
                              arguments={'token': 'PRIVATE'})
                if not completed.wait(2):
                    raise RuntimeError('child progress was not received before completion')

        self.runner.callback = callback
        with self.service.turn(self.parent, 10), observe_progress(notify):
            result = delegate(self.parent, self.roles['tech_lead'], 'backend_engineer')
        self.assertTrue(completed.is_set(), received)
        self.assertEqual(result.get('state'), 'TASK_STATE_COMPLETED', result)
        self.assertEqual(sum(json.loads(m).get('agent_role') == 'qa_engineer' and json.loads(m).get('name') == 'mcp__github__pull_request_read' for m in received), 1)
        self.assertIn({'event': 'model_request', 'agent_role': 'backend_engineer'}, [json.loads(m) for m in received])
        self.assertNotIn('PRIVATE', json.dumps(received))
        wait_for(lambda: not self.service.jobs)
        self.assertTrue(all(chat.closed for chat in self.runner.chats))

    def test_child_activity_is_bounded_and_cannot_be_read_by_another_parent(self):
        ready, finish = threading.Event(), threading.Event()
        self.addCleanup(finish.set)
        def callback(*_):
            for _ in range(100):
                emit_progress('model_request')
                emit_progress('tool_call', name='read_file', arguments={'path': 'app.py', 'token': 'PRIVATE'})
            ready.set()
            finish.wait(3)
        self.runner.callback = callback
        other = '1111111111111111'
        other_env = self.service.worker_environment(other, self.roles['tech_lead'])
        with self.service.turn(self.parent, 5), self.service.turn(other, 5):
            task_id = self.send()['id']
            self.assertTrue(ready.wait(2))
            task = self.service.dispatch(self.token, 'backend_engineer', 'GetTask', {'id': task_id})
            activity = task['metadata'].get('activity', [])
            self.assertEqual(len(activity), 64)
            self.assertTrue(all(json.loads(a['message']).get('agent_role') == 'backend_engineer' for a in activity))
            self.assertEqual([a['seq'] for a in activity], sorted(set(a['seq'] for a in activity)))
            with self.assertRaises(A2AError):
                self.service.dispatch(other_env['AX_DELEGATION_TOKEN'], 'backend_engineer',
                                      'GetTask', {'id': task_id})
            finish.set()
            task = self.wait_task(task_id)
            self.assertEqual(task['status']['state'], 'TASK_STATE_COMPLETED')
            self.assertIn('activity', task['metadata'])
            self.assertNotIn('PRIVATE', json.dumps(task))

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

    def test_children_wait_in_shared_worker_queue_without_separate_capacity_rejection(self):
        runner = AxAgentRunner('localhost:5001/popot-agent-ax@sha256:' + 'a' * 64,
                               'unused', 'http://localhost:1/v1')
        runner.delegation = self.service
        self.service.runners = {"test": runner}
        leases = [runner.worker_queue.acquire(f'occupied-{index}') for index in range(10)]
        def cli(*args, **_kwargs):
            return subprocess.CompletedProcess([], 1 if args[0] == 'get' else 0, '',
                                               'NotFound' if args[0] == 'get' else '')
        with patch.object(runner, '_run', side_effect=cli) as run, \
             patch.object(runner, '_wait_for_chat', side_effect=lambda chat, *_args: chat), \
             patch.object(AxChat, 'send', return_value={'answer': 'child result'}):
            try:
                with self.service.turn(self.parent, 5):
                    tasks = [self.send(message_id=f'child-{index}') for index in range(4)]
                    wait_for(lambda: runner.worker_queue.status()['queued'] == 4)
                    self.assertEqual(runner.worker_queue.status()['active'], 10)
                    run.assert_not_called()
                    runner.worker_queue.release('occupied-0', leases[0])
                    for task in tasks:
                        result = self.wait_task(task['id'])
                        self.assertEqual(result['status']['state'], 'TASK_STATE_COMPLETED')
                    wait_for(lambda: not self.service.jobs)
                    self.assertEqual(runner.worker_queue.status()['active'], 9)
                    self.assertEqual(runner.worker_queue.status()['queued'], 0)
            finally:
                runner.shutdown_worker_queue()
                self.service.close()

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
            self.assertEqual(self.service.worker_environment(chat.session_id, chat.config), {})
            # Even a caller holding a previously issued capability cannot bypass the depth limit.
            token = self.service.grants[chat.session_id].token
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

    def test_catalog_uses_current_descriptions_and_excludes_ancestors(self):
        import json
        self.roles["qa_engineer"]["description"] = "Plan regression checks."
        config = {**self.roles["backend_engineer"], "_ax_delegation": {
            "root_session_id": self.parent, "lineage": ["tech_lead", "backend_engineer"]}}
        env = self.service.worker_environment("2222222222222222", config)
        self.assertEqual(json.loads(env["AX_DELEGATION_ROLES"]), ["qa_engineer"])
        self.assertEqual(json.loads(env["AX_DELEGATION_DESCRIPTIONS"]),
                         {"qa_engineer": "Plan regression checks."})
        self.assertEqual(self.service.card("qa_engineer")["description"], "Plan regression checks.")

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

    def test_harness_routes_pr_comment_followup_through_actual_a2a(self):
        import os
        from ax_local.worker.delegation import make_tools
        from popot_agents.worker.http_harness import run_http
        self.roles['code_reviewer'] = {'agent': 'test', 'instructions': 'review', 'tools': [],
                                       '_ax_role': 'code_reviewer', 'allowed_roles': []}
        self.roles['backend_engineer']['allowed_roles'].append('code_reviewer')
        parent = '1111111111111111'
        environment = self.service.worker_environment(parent, self.roles['backend_engineer'])
        server = start_a2a_server(self.service, '127.0.0.1', 0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        tools = make_tools({**environment,
                           'AX_DELEGATION_URL': f'http://127.0.0.1:{server.server_port}',
                           'AX_DELEGATION_POLL_SECONDS': '0.01'})
        pr = 'https://github.com/example/project/pull/70'
        history = [{'role': 'user', 'content': 'Review ' + pr + '; do not merge.'},
                   {'role': 'assistant', 'content': 'Old unverified findings.'},
                   {'role': 'user', 'content': 'оставь комментарии в PR'}]
        with self.service.turn(parent, 5), \
             patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1',
                                     'HARNESS_MODEL': 'fake'}, clear=True), \
             patch('popot_agents.worker.http_harness.request.urlopen') as parent_model, \
             redirect_stderr(io.StringIO()):
            answer = run_http(history, self.roles['backend_engineer'], extra_tools=tools)
        parent_model.assert_not_called()
        self.assertEqual(len(self.runner.chats), 1)
        child = self.runner.chats[0]
        self.assertTrue(child.closed)
        saved = self.store.get(child.session_id)
        self.assertEqual(saved['role'], 'code_reviewer')
        self.assertEqual(saved['roleConfig']['_ax_delegation']['parent_session_id'], parent)
        self.assertIn(pr, saved['messages'][0]['content'])
        self.assertIn('do not merge', saved['messages'][0]['content'])
        self.assertIn(child.session_id, answer)

    def test_worker_retries_lost_submission_response_without_duplicate_actor(self):
        import json
        import httpx
        from ax_local.worker.delegation import make_tools
        server = start_a2a_server(self.service, "127.0.0.1", 0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        tools = make_tools({"AX_DELEGATION_URL": f"http://127.0.0.1:{server.server_port}",
                            "AX_DELEGATION_TOKEN": self.token,
                            "AX_DELEGATION_ROLES": '["backend_engineer"]',
                            "AX_DELEGATION_POLL_SECONDS": "0.01"})
        from ax_local.worker.delegation import BoundedHTTPTransport
        original = BoundedHTTPTransport.handle_async_request
        messages = []
        async def drop_response(transport, call):
            response = await original(transport, call)
            body = json.loads(call.content)
            if body["method"] == "SendMessage":
                messages.append(body["params"]["message"]["messageId"])
                if len(messages) == 1:
                    await response.aclose()
                    raise httpx.ReadError("lost response", request=call)
            return response
        with self.service.turn(self.parent, 5), \
             patch.object(BoundedHTTPTransport, "handle_async_request", new=drop_response):
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

    def test_protocol_validation_rejects_malformed_history_length_before_starting_worker(self):
        from urllib import request
        server = start_a2a_server(self.service, "127.0.0.1", 0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        body = {"jsonrpc": "2.0", "id": "bad-history", "method": "SendMessage", "params": {
            "message": {"messageId": "bad-history", "role": "ROLE_USER",
                        "parts": [{"text": "must not run"}]},
            "configuration": {"returnImmediately": True, "historyLength": "invalid"}}}
        call = request.Request(f"http://127.0.0.1:{server.server_port}/a2a/backend_engineer",
                               data=json.dumps(body).encode(), headers={
            "Content-Type": "application/json", "A2A-Version": "1.0",
            "Authorization": "Bearer " + self.token})
        with self.service.turn(self.parent, 5), request.urlopen(call) as response:
            result = json.load(response)
            self.assertIn("error", result)
            self.assertEqual(result["error"]["code"], -32602)
            self.assertEqual(self.runner.chats, [])

    def sdk_server(self):
        server = start_a2a_server(self.service, "127.0.0.1", 0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.service.base_url = f"http://127.0.0.1:{server.server_port}"
        return self.service.base_url + "/a2a/backend_engineer"

    def test_official_sdk_discovers_card_submits_polls_and_cancels(self):
        import httpx
        from a2a.client.card_resolver import A2ACardResolver
        from a2a.client.transports.jsonrpc import JsonRpcTransport
        from a2a.types import a2a_pb2 as types
        from a2a.utils.errors import TaskNotFoundError, UnsupportedOperationError
        url = self.sdk_server()
        release = threading.Event()
        self.addCleanup(release.set)
        self.runner.callback = lambda *_: release.wait(2)

        async def check():
            async with httpx.AsyncClient(headers={"A2A-Version": "1.0",
                     "Authorization": "Bearer " + self.token}, trust_env=False) as http:
                card = await A2ACardResolver(http, url).get_agent_card()
                self.assertEqual(card.supported_interfaces[0].protocol_version, "1.0")
                self.assertEqual(card.supported_interfaces[0].url, url)
                transport = JsonRpcTransport(http, card, card.supported_interfaces[0].url)
                params = types.SendMessageRequest(message=types.Message(
                    message_id="sdk-cancel", role=types.ROLE_USER,
                    parts=[types.Part(text="wait for cancellation")]),
                    configuration=types.SendMessageConfiguration(return_immediately=True))
                task = (await transport.send_message(params)).task
                again = (await transport.send_message(params)).task
                self.assertEqual(task.id, again.id)
                with self.assertRaises(TaskNotFoundError):
                    await transport.get_task(types.GetTaskRequest(id="unknown"))
                with self.assertRaises(UnsupportedOperationError):
                    await transport.list_tasks(types.ListTasksRequest())
                canceled = await transport.cancel_task(types.CancelTaskRequest(id=task.id))
                self.assertEqual(canceled.status.state, types.TASK_STATE_CANCELED)
                polled = await transport.get_task(types.GetTaskRequest(id=task.id))
                self.assertEqual(polled.status.state, types.TASK_STATE_CANCELED)
                release.set()
        with self.service.turn(self.parent, 5):
            asyncio.run(check())
        self.assertLessEqual(len(self.runner.chats), 1)
        self.assertTrue(all(chat.closed for chat in self.runner.chats))

    def test_sdk_blocking_submission_waits_for_result_without_blocking_other_requests(self):
        import httpx
        from a2a.client.transports.jsonrpc import JsonRpcTransport
        from a2a.types import a2a_pb2 as types
        url = self.sdk_server()
        release = threading.Event()
        self.addCleanup(release.set)
        self.runner.callback = lambda *_: release.wait(2)

        async def check():
            async with httpx.AsyncClient(headers={"A2A-Version": "1.0",
                     "Authorization": "Bearer " + self.token}, trust_env=False) as http:
                client = JsonRpcTransport(http, types.AgentCard(), url)
                pending = asyncio.create_task(client.send_message(types.SendMessageRequest(
                    message=types.Message(message_id="sdk-blocking", role=types.ROLE_USER,
                                          parts=[types.Part(text="blocking result")]))))
                self.assertTrue(await asyncio.to_thread(self.runner.entered.wait, 1))
                self.assertFalse(pending.done())
                task_id = self.runner.chats[0].session_id
                polled = await asyncio.wait_for(client.get_task(
                    types.GetTaskRequest(id=task_id)), timeout=1)
                self.assertEqual(polled.status.state, types.TASK_STATE_WORKING)
                release.set()
                result = await asyncio.wait_for(pending, timeout=1)
                self.assertEqual(result.task.status.state, types.TASK_STATE_COMPLETED)
                self.assertEqual(result.task.artifacts[0].parts[0].text, "child result: blocking result")
        with self.service.turn(self.parent, 5):
            asyncio.run(check())

    def test_http_authority_version_and_request_size_are_enforced(self):
        import httpx
        url = self.sdk_server()
        body = {"jsonrpc": "2.0", "id": "guard", "method": "SendMessage", "params": {
            "message": {"messageId": "guard", "role": "ROLE_USER", "parts": [{"text": "deny"}]},
            "configuration": {"returnImmediately": True}}}
        with httpx.Client(trust_env=False) as http:
            self.assertEqual(http.post(url, json=body).status_code, 403)
            with self.service.turn(self.parent, 5):
                headers = {"A2A-Version": "1.0", "Authorization": "Bearer " + self.token}
                self.assertEqual(http.post(url.replace("backend_engineer", "qa_engineer"),
                                           json=body, headers=headers).status_code, 403)
                wrong_version = http.post(url, json=body, headers={**headers, "A2A-Version": "2.0"})
                self.assertEqual(wrong_version.json()["error"]["code"], -32009)
                with patch.dict("ax_local.api.a2a_sdk.RUNTIME", {"limits": {"request_bytes": 16}}):
                    self.assertEqual(http.post(url, content=b"x" * 17, headers=headers).status_code, 413)
                self.assertEqual(self.runner.chats, [])

    def test_sdk_cannot_read_another_parents_child_or_expose_storage_exceptions(self):
        import httpx
        url = self.sdk_server()
        other = "1111111111111111"
        other_token = self.service.worker_environment(other, self.roles["tech_lead"])["AX_DELEGATION_TOKEN"]
        with self.service.turn(self.parent, 5), self.service.turn(other, 5):
            task = self.wait_task(self.send()["id"])
            body = {"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": task["id"]}}
            with httpx.Client(trust_env=False) as http:
                headers = {"A2A-Version": "1.0", "Authorization": "Bearer " + other_token}
                self.assertEqual(http.post(url, json=body, headers=headers).json()["error"]["code"], -32001)
                with patch.object(self.store, "get", side_effect=OSError("private-db-credential")):
                    response = http.post(url, json=body, headers=headers)
                    self.assertEqual(response.json()["error"]["code"], -32603)
                    self.assertNotIn("private-db-credential", response.text)

    def test_worker_deadline_sends_sdk_cancel_and_releases_child(self):
        from ax_local.worker.delegation import make_tools
        url = self.sdk_server()
        release = threading.Event()
        self.addCleanup(release.set)
        self.runner.callback = lambda *_: release.wait(2)
        tools = make_tools({"AX_DELEGATION_URL": url.rsplit("/a2a/", 1)[0],
                           "AX_DELEGATION_TOKEN": self.token,
                           "AX_DELEGATION_ROLES": '["backend_engineer"]',
                           "AX_DELEGATION_POLL_SECONDS": "0.01"})
        with self.service.turn(self.parent, 5):
            started = time.monotonic()
            result = json.loads(tools["delegate_task"]["call"](
                {"role": "backend_engineer", "task": "timeout"},
                timeout_seconds=0.2, call_id="deadline"))
            self.assertLess(time.monotonic() - started, 1)
            self.assertIn("error", result)
            self.assertEqual(self.store.get(result["sessionId"])["roleConfig"]["_ax_delegation"]["state"],
                             "TASK_STATE_CANCELED")
            self.assertTrue(self.runner.chats[0].closed)
            release.set()

    def test_nested_workers_delegate_over_sdk_http(self):
        from ax_local.worker.delegation import make_tools
        self.sdk_server()
        results = []
        def callback(chat, _task):
            if chat.config["_ax_role"] == "backend_engineer":
                env = self.service.worker_environment(chat.session_id, chat.config)
                env["AX_DELEGATION_POLL_SECONDS"] = "0.01"
                results.append(json.loads(make_tools(env)["delegate_task"]["call"](
                    {"role": "qa_engineer", "task": "nested SDK"},
                    timeout_seconds=2, call_id="nested")))
        self.runner.callback = callback
        env = self.service.worker_environment(self.parent, self.roles["tech_lead"])
        env["AX_DELEGATION_POLL_SECONDS"] = "0.01"
        with self.service.turn(self.parent, 5):
            result = json.loads(make_tools(env)["delegate_task"]["call"](
                {"role": "backend_engineer", "task": "delegate onward"},
                timeout_seconds=3, call_id="parent"))
        self.assertEqual(result["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(results[0]["answer"], "child result: nested SDK")
        self.assertEqual(len(self.runner.chats), 2)
        self.assertTrue(all(chat.closed for chat in self.runner.chats))


if __name__ == "__main__":
    unittest.main()

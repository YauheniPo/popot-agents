import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ax_local.api.cron import CronScheduler, cron_matches, validate_cron
from popot_agents.orchestrator.main import create_server
from popot_agents.orchestrator.session_store import SessionStore


class FakeChat:
    def __init__(self):
        self.closed = False

    def send(self, task):
        return {"answer": "done: " + task}

    def close(self):
        self.closed = True


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.chats = []

    def start_chat(self, session_id, config, **_kwargs):
        self.calls.append((session_id, config))
        chat = FakeChat()
        self.chats.append(chat)
        return chat

    def close_session(self, session_id):
        pass


class CronTests(unittest.TestCase):
    def test_cron_matches_utc_minute_and_sunday(self):
        self.assertTrue(cron_matches("*/15 9-17 * * 1-5", datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)))
        self.assertFalse(cron_matches("*/15 9-17 * * 1-5", datetime(2026, 10, 1, 9, 31, tzinfo=timezone.utc)))
        self.assertTrue(cron_matches("0 0 * * 0", datetime(2026, 10, 4, 0, 0, tzinfo=timezone.utc)))
        self.assertTrue(cron_matches("0 0 1 * 1", datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)))

    def test_wildcard_step_keeps_day_fields_conjunctive(self):
        first = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
        second = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)
        self.assertTrue(cron_matches("0 9 1 * */1", first))
        self.assertFalse(cron_matches("0 9 1 * */1", second))
        self.assertFalse(cron_matches("0 9 */1 * 4", second))

    def test_invalid_cron_is_rejected(self):
        for expression in ("* * * *", "60 * * * *", "*/0 * * * *", "* * * * MON", "* * * * 8"):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                validate_cron(expression)

    def test_due_run_persists_result_and_does_not_repeat_same_minute(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            runner = FakeRunner()
            roles = {"analyst": {"agent": "openrouter", "instructions": "Analyze", "tools": [],
                                 "cron": [{"schedule": "* * * * *", "task": "report"}]}}
            scheduler = CronScheduler(roles, runner, store, max_concurrent_tasks=1)
            now = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
            scheduler.run_due(now)
            scheduler.wait_for_jobs()
            scheduler.run_due(now)
            self.assertEqual(len(runner.calls), 1)
            self.assertTrue(runner.chats[0].closed)
            saved = store.list_chats()[0]
            self.assertEqual(saved["messages"], [{"role": "user", "content": "report"},
                                                 {"role": "assistant", "content": "done: report"}])
            self.assertEqual(saved["roleConfig"]["_ax_cron"]["state"], "TASK_STATE_COMPLETED")
            self.assertEqual(saved["roleConfig"]["_ax_cron"]["scheduled_at"], "2026-10-01T10:00:00+00:00")
            self.assertNotIn("cron", runner.calls[0][1])
            scheduler.close()
            restarted = CronScheduler(roles, FakeRunner(), store, max_concurrent_tasks=1)
            restarted.run_due(now)
            self.assertEqual(len(store.list_chats()), 1)
            restarted.close()

    def test_failed_worker_is_saved_and_closed(self):
        class FailingChat(FakeChat):
            def send(self, task):
                raise RuntimeError("model failed")

        class FailingRunner(FakeRunner):
            def start_chat(self, session_id, config, **_kwargs):
                chat = FailingChat()
                self.chats.append(chat)
                return chat

        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            runner = FailingRunner()
            roles = {"analyst": {"agent": "openrouter", "instructions": "Analyze", "tools": [],
                                 "cron": [{"schedule": "* * * * *", "task": "report"}]}}
            scheduler = CronScheduler(roles, runner, store, max_concurrent_tasks=1)
            scheduler.run_due(datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc))
            scheduler.wait_for_jobs()
            saved = store.list_chats()[0]
            self.assertEqual(saved["roleConfig"]["_ax_cron"]["state"], "TASK_STATE_FAILED")
            self.assertEqual(saved["roleConfig"]["_ax_cron"]["stage"], "worker_message")
            self.assertEqual(saved["messages"], [])
            self.assertTrue(runner.chats[0].closed)
            scheduler.close()

    def test_active_cron_chat_cannot_be_deleted_until_owner_finishes(self):
        entered, release = threading.Event(), threading.Event()

        class BlockingChat(FakeChat):
            def send(self, task):
                entered.set()
                release.wait(2)
                return super().send(task)

        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(directory)
            runner = FakeRunner()
            runner.start_chat = lambda session_id, config, **kwargs: BlockingChat()
            roles = {"analyst": {"agent": "openrouter", "instructions": "Analyze", "tools": [],
                                 "cron": [{"schedule": "* * * * *", "task": "report"}]}}
            scheduler = CronScheduler(roles, runner, store, max_concurrent_tasks=1)
            with patch("popot_agents.orchestrator.main.ChatServer") as server_class:
                create_server({"openrouter": runner})
            handler_class = server_class.call_args.args[1]
            server = SimpleNamespace(store=store, live={}, live_lock=threading.RLock(),
                                     session_lock=lambda _session_id: threading.RLock(),
                                     delegation=None, cron=scheduler)

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
                scheduler.run_due(datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc))
                self.assertTrue(entered.wait(2))
                session_id = store.list_chats()[0]["sessionId"]
                self.assertEqual(delete(session_id)[0], 409)
                self.assertIsNotNone(store.get(session_id))
                release.set()
                scheduler.wait_for_jobs()
                self.assertEqual(store.get(session_id)["roleConfig"]["_ax_cron"]["state"],
                                 "TASK_STATE_COMPLETED")
                self.assertEqual(delete(session_id), (200, {"status": "closed"}))
                self.assertIsNone(store.get(session_id))
            finally:
                release.set()
                scheduler.wait_for_jobs()
                scheduler.close()


if __name__ == "__main__":
    unittest.main()

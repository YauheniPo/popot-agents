import json
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from popot_agents.orchestrator.main import AgentRunError, DockerAgentRunner, create_server
from popot_agents.worker.session_worker import Conversation


class FakeChat:
    def __init__(self, name):
        self.container_name = name
        self.messages = []
        self.closed = False
        self.restored = []

    def send(self, message):
        if message == "fail":
            raise AgentRunError("provider failed")
        self.messages.append(message)
        return {"answer": f"turn {len(self.messages)}"}

    def close(self):
        self.closed = True

    def restore(self, history):
        self.restored = [dict(item) for item in history]
        self.messages = [item["content"] for item in history if item["role"] == "user"]

    def is_alive(self):
        return not self.closed


class FakeRunner:
    def __init__(self):
        self.chats = []

    def __call__(self, task):
        return {"answer": task}

    def start_chat(self, chat_id):
        chat = FakeChat(f"worker-{chat_id}")
        self.chats.append(chat)
        return chat


class ChatApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.runner = FakeRunner()
        self.server = create_server({"test_agent": self.runner}, port=0, session_dir=self.directory.name)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def request(self, method, path, payload=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port)
        body = json.dumps(payload) if payload is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        status = response.status
        data = json.loads(response.read())
        connection.close()
        return status, data

    def test_chat_can_be_inspected_and_closed(self):
        _, first = self.request("POST", "/messages", {"agent": "test_agent", "message": "hello"})
        session_id = first["sessionId"]
        self.assertEqual(first["container"], f"worker-{session_id}")
        status, info = self.request("GET", f"/chats/{session_id}")
        self.assertEqual(status, 200)
        self.assertEqual(info["container"], first["container"])
        self.assertEqual(info["turns"], 1)
        status, listing = self.request("GET", "/chats")
        self.assertEqual(status, 200)
        self.assertIn(session_id, [item["sessionId"] for item in listing["chats"]])
        self.assertEqual(self.request("DELETE", f"/chats/{session_id}"), (200, {"status": "closed"}))
        self.assertTrue(self.runner.chats[0].closed)
        self.assertEqual(self.request("GET", f"/chats/{session_id}")[0], 404)

    def test_unknown_chat_is_not_created_implicitly(self):
        status, _ = self.request("POST", "/messages", {"sessionId": "missing", "message": "hello"})
        self.assertEqual(status, 404)
        self.assertEqual(len(self.runner.chats), 0)

    def test_new_chat_requires_agent_or_role(self):
        status, data = self.request("POST", "/messages", {"message": "hello"})
        self.assertEqual(status, 400)
        self.assertIn("agent or role", data["error"])
        self.assertEqual(len(self.runner.chats), 0)

    def test_messages_create_chat_without_session_id_and_reuse_it_with_id(self):
        status, first = self.request("POST", "/messages", {"agent": "test_agent", "message": "hello"})
        self.assertEqual(status, 200)
        self.assertEqual(first["answer"], "turn 1")
        self.assertEqual(len(self.runner.chats), 1)

        status, followup = self.request("POST", "/messages", {"sessionId": first["sessionId"], "message": "continue"})
        self.assertEqual(status, 200)
        self.assertEqual(followup["answer"], "turn 2")
        self.assertEqual(followup["container"], first["container"])
        self.assertEqual(len(self.runner.chats), 1)

        status, second = self.request("POST", "/messages", {"agent": "test_agent", "message": "new chat"})
        self.assertEqual(status, 200)
        self.assertNotEqual(second["sessionId"], first["sessionId"])
        self.assertEqual(len(self.runner.chats), 2)

    def test_unknown_session_id_does_not_start_a_worker(self):
        status, response = self.request("POST", "/messages", {"sessionId": "missing", "message": "hello"})
        self.assertEqual(status, 404)
        self.assertIn("session", response["error"])
        self.assertEqual(self.runner.chats, [])

    def test_stopped_chat_restores_history_in_a_new_worker(self):
        _, first = self.request("POST", "/messages", {"agent": "test_agent", "message": "remember 137"})
        session_id = first["sessionId"]
        old_worker = self.runner.chats[0]
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.assertTrue(old_worker.closed)

        self.server = create_server({"test_agent": self.runner}, port=0, session_dir=self.directory.name)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        status, info = self.request("GET", f"/chats/{session_id}")
        self.assertEqual(status, 200)
        self.assertEqual(info["status"], "stopped")

        status, followup = self.request("POST", "/messages", {"sessionId": session_id, "message": "what number?"})
        self.assertEqual(status, 200)
        self.assertEqual(followup["answer"], "turn 2")
        self.assertIsNot(self.runner.chats[1], old_worker)
        self.assertEqual(self.runner.chats[1].restored, [
            {"role": "user", "content": "remember 137"},
            {"role": "assistant", "content": "turn 1"},
        ])

    def test_saved_chat_reports_missing_profile_after_configuration_change(self):
        _, first = self.request("POST", "/messages", {"agent": "test_agent", "message": "hello"})
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.server = create_server({"other": FakeRunner()}, port=0, session_dir=self.directory.name)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        status, response = self.request("POST", "/messages", {
            "sessionId": first["sessionId"], "message": "continue",
        })
        self.assertEqual(status, 503)
        self.assertIn("profile", response["error"])

    def test_stopped_worker_recovers_on_first_followup(self):
        _, first = self.request("POST", "/messages", {"agent": "test_agent", "message": "remember 137"})
        self.runner.chats[0].close()
        status, followup = self.request("POST", "/messages", {
            "sessionId": first["sessionId"], "message": "what number?",
        })
        self.assertEqual(status, 200)
        self.assertEqual(followup["answer"], "turn 2")
        self.assertEqual(len(self.runner.chats), 2)

    def test_model_error_keeps_live_worker_and_saved_history(self):
        _, first = self.request("POST", "/messages", {"agent": "test_agent", "message": "hello"})
        session_id = first["sessionId"]
        status, _ = self.request("POST", "/messages", {"sessionId": session_id, "message": "fail"})
        self.assertEqual(status, 502)
        status, answer = self.request("POST", "/messages", {"sessionId": session_id, "message": "continue"})
        self.assertEqual(status, 200)
        self.assertEqual(answer["answer"], "turn 2")
        self.assertEqual(len(self.runner.chats), 1)

    def test_idle_worker_stops_but_session_remains_resumable(self):
        _, first = self.request("POST", "/messages", {"agent": "test_agent", "message": "hello"})
        session_id = first["sessionId"]
        self.server.live[session_id].last_used = time.monotonic() - 1900
        self.server.service_actions()
        self.assertTrue(self.runner.chats[0].closed)
        status, info = self.request("GET", f"/chats/{session_id}")
        self.assertEqual(status, 200)
        self.assertEqual(info["status"], "stopped")

    def test_session_creation_time_survives_messages_and_server_restart(self):
        _, first = self.request("POST", "/messages", {"agent": "test_agent", "message": "hello"})
        session_id = first["sessionId"]
        path = Path(self.directory.name) / f"{session_id}.json"
        created_at = json.loads(path.read_text(encoding="utf-8"))["createdAt"]
        self.request("POST", "/messages", {"sessionId": session_id, "message": "again"})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["createdAt"], created_at)

        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.server = create_server({"test_agent": self.runner}, port=0, session_dir=self.directory.name)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.request("POST", "/messages", {"sessionId": session_id, "message": "later"})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["createdAt"], created_at)

    def test_session_expires_seven_days_after_creation_even_if_recently_used(self):
        _, first = self.request("POST", "/messages", {"agent": "test_agent", "message": "hello"})
        session_id = first["sessionId"]
        path = Path(self.directory.name) / f"{session_id}.json"
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved["createdAt"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        saved["updatedAt"] = datetime.now(timezone.utc).isoformat()
        replacement = path.with_suffix(".tmp")
        replacement.write_text(json.dumps(saved), encoding="utf-8")
        replacement.replace(path)

        status, _ = self.request("POST", "/messages", {
            "sessionId": session_id, "message": "continue",
        })
        self.assertEqual(status, 404)
        self.server.last_prune = 0
        self.server.service_actions()
        self.assertTrue(self.runner.chats[0].closed)
        self.assertFalse(path.exists())
        self.assertNotIn(session_id, [chat["sessionId"] for chat in self.request("GET", "/chats")[1]["chats"]])

    def test_old_session_file_without_created_at_remains_resumable(self):
        _, first = self.request("POST", "/messages", {"agent": "test_agent", "message": "hello"})
        session_id = first["sessionId"]
        path = Path(self.directory.name) / f"{session_id}.json"
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved.pop("createdAt")
        replacement = path.with_suffix(".tmp")
        replacement.write_text(json.dumps(saved), encoding="utf-8")
        replacement.replace(path)
        status, reply = self.request("POST", "/messages", {
            "sessionId": session_id, "message": "continue",
        })
        self.assertEqual(status, 200)
        self.assertEqual(reply["answer"], "turn 2")
        migrated = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(migrated["createdAt"], saved["updatedAt"])

    def test_storage_failure_closes_new_worker(self):
        with patch.object(self.server.store, "save", side_effect=OSError("disk full")):
            status, _ = self.request("POST", "/messages", {"agent": "test_agent", "message": "hello"})
        self.assertEqual(status, 500)
        self.assertTrue(self.runner.chats[0].closed)
        self.assertEqual(self.server.live, {})


class ConversationTests(unittest.TestCase):
    def test_preserves_full_history_and_commits_only_successful_turns(self):
        received = []

        def answer(messages):
            received.append([dict(item) for item in messages])
            if messages[-1]["content"] == "fail":
                raise RuntimeError("model failed")
            return f"reply {len(received)}"

        conversation = Conversation(answer)
        self.assertEqual(conversation.ask("remember 137"), "reply 1")
        with self.assertRaisesRegex(RuntimeError, "model failed"):
            conversation.ask("fail")
        self.assertEqual(conversation.ask("what number?"), "reply 3")
        self.assertEqual(received[-1], [
            {"role": "user", "content": "remember 137"},
            {"role": "assistant", "content": "reply 1"},
            {"role": "user", "content": "what number?"},
        ])

    def test_restored_history_is_used_by_next_turn(self):
        received = []
        conversation = Conversation(lambda messages: received.extend(messages) or "continued")
        conversation.restore([
            {"role": "user", "content": "remember 137"},
            {"role": "assistant", "content": "I will remember"},
        ])
        self.assertEqual(conversation.ask("what number?"), "continued")
        self.assertEqual([item["content"] for item in received], [
            "remember 137", "I will remember", "what number?",
        ])

    def test_oversized_answer_does_not_save_unrestorable_history(self):
        with patch("popot_agents.worker.session_worker.MAX_HISTORY_BYTES", 200):
            conversation = Conversation(lambda messages: "x" * 300)
            with self.assertRaisesRegex(ValueError, "history"):
                conversation.ask("hello")
            self.assertEqual(conversation.messages, [])


class DockerChatTests(unittest.TestCase):
    @patch("popot_agents.orchestrator.main.subprocess.run")
    def test_starts_one_container_then_executes_messages_and_stops_it(self, run):
        run.side_effect = [
            subprocess.CompletedProcess([], 0, "container-id\n", ""),
            subprocess.CompletedProcess([], 0, '{"status":"ok"}', ""),
            subprocess.CompletedProcess([], 0, '{"status":"ok"}', ""),
            subprocess.CompletedProcess([], 0, '{"answer":"hello"}', ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
        runner = DockerAgentRunner(network="bridge")
        chat = runner.start_chat("abc123")
        chat.restore([{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "reply"}])
        self.assertEqual(chat.send("hi"), {"answer": "hello"})
        chat.close()
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][:3], ["docker", "run", "--rm"])
        self.assertIn("--detach", commands[0])
        self.assertIn("popot.chat_id=abc123", commands[0])
        self.assertEqual(commands[2][:3], ["docker", "exec", "--interactive"])
        self.assertEqual(commands[3][:3], ["docker", "exec", "--interactive"])
        self.assertEqual(commands[4][:3], ["docker", "rm", "-f"])

    @patch("popot_agents.orchestrator.main.subprocess.run")
    def test_reconnects_owned_container_after_api_restart(self, run):
        run.side_effect = [
            subprocess.CompletedProcess([], 125, "", "container name already in use"),
            subprocess.CompletedProcess([], 0, "abc123\n", ""),
            subprocess.CompletedProcess([], 0, '{"status":"ok"}', ""),
        ]
        chat = DockerAgentRunner(network="bridge").start_chat("abc123")
        self.assertEqual(chat.container_name, "popot-chat-abc123")
        self.assertEqual(run.call_args_list[1].args[0][:2], ["docker", "inspect"])


if __name__ == "__main__":
    unittest.main()

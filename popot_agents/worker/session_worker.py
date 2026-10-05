"""One long-lived conversation process inside one Docker chat container."""

import json
import os
import socket
import socketserver
import sys
import time

from .agent_worker import run_harness
from .http_harness import run_http
from popot_agents.runtime_config import RUNTIME


SOCKET_PATH = os.getenv("HARNESS_SOCKET_PATH", "/workspace/chat.sock")
MAX_HISTORY_BYTES = RUNTIME["limits"]["history_bytes"]


def _trace(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False),
          file=sys.stderr, flush=True)


class Conversation:
    def __init__(self, answer):
        self.answer = answer
        self.messages = []

    def ask(self, message: str) -> str:
        if not isinstance(message, str) or not message.strip() \
                or len(message) > RUNTIME["limits"]["message_chars"]:
            raise ValueError(f"message must be a nonempty string up to {RUNTIME['limits']['message_chars']} characters")
        candidate = self.messages + [{"role": "user", "content": message.strip()}]
        if len(json.dumps(candidate, ensure_ascii=False).encode("utf-8")) > MAX_HISTORY_BYTES:
            raise ValueError("chat history is full; start a new chat")
        turn = len(self.messages) // 2 + 1
        started_at = time.monotonic()
        _trace("turn_started", turn=turn)
        try:
            answer = self.answer(candidate)
            if not isinstance(answer, str) or not answer.strip():
                raise RuntimeError("harness returned an empty answer")
            completed = candidate + [{"role": "assistant", "content": answer}]
            if len(json.dumps(completed, ensure_ascii=False).encode("utf-8")) > MAX_HISTORY_BYTES:
                raise ValueError("chat history is full; start a new chat")
            self.messages = completed
        except Exception as exc:
            _trace("turn_failed", turn=turn, error_type=type(exc).__name__,
                   elapsed_seconds=round(time.monotonic() - started_at, 1))
            raise
        _trace("turn_completed", turn=turn, answer_chars=len(answer),
               elapsed_seconds=round(time.monotonic() - started_at, 1))
        return answer

    def restore(self, messages: list[dict[str, str]]) -> None:
        if not isinstance(messages, list) or len(messages) % 2:
            raise ValueError("invalid chat history")
        for index, item in enumerate(messages):
            expected = "user" if index % 2 == 0 else "assistant"
            if (not isinstance(item, dict) or item.get("role") != expected
                    or not isinstance(item.get("content"), str)):
                raise ValueError("invalid chat history")
        if len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) > MAX_HISTORY_BYTES:
            raise ValueError("chat history is full")
        self.messages = [dict(item) for item in messages]


def answer_with_cli(messages: list[dict[str, str]], instructions: str = "") -> str:
    if len(messages) == 1:
        prompt = messages[0]["content"]
    else:
        prompt = "\n\n".join(
            f"{item['role'].capitalize()}: {item['content']}" for item in messages
        ) + "\n\nAssistant:"
    if instructions:
        prompt = f"Instructions: {instructions}\n\n{prompt}"
    return run_harness(prompt)


def serve(*, extra_tools: dict | None = None) -> None:
    role_config = json.loads(os.getenv("HARNESS_ROLE_JSON", "{}"))
    if os.getenv("HARNESS_SESSION_MODE") == "http":
        answer = lambda messages: run_http(messages, role_config, extra_tools=extra_tools)
    else:
        answer = lambda messages: answer_with_cli(messages, role_config.get("instructions", ""))
    conversation = Conversation(answer)

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            try:
                payload = json.loads(self.rfile.readline(RUNTIME["limits"]["socket_message_bytes"]))
                if not isinstance(payload, dict):
                    raise ValueError("JSON object is required")
                if payload.get("action") == "ping":
                    result = {"status": "ok"}
                elif payload.get("action") == "message":
                    result = {"answer": conversation.ask(payload.get("message"))}
                elif payload.get("action") == "restore":
                    conversation.restore(payload.get("messages"))
                    _trace("session_restored", turns=len(conversation.messages) // 2)
                    result = {"status": "ok"}
                else:
                    result = {"error": "unknown action"}
            except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
                _trace("worker_error", error_type=type(exc).__name__)
                result = {"error": str(exc)}
            self.wfile.write((json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"))

    with socketserver.UnixStreamServer(SOCKET_PATH, Handler) as server:
        _trace("worker_ready", session_mode=os.getenv("HARNESS_SESSION_MODE", "cli"))
        server.serve_forever()


def client(action: str) -> None:
    payload = {"action": action}
    if action in {"message", "restore"}:
        payload.update(json.load(sys.stdin))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(SOCKET_PATH)
        connection.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        result = b""
        while not result.endswith(b"\n"):
            chunk = connection.recv(65536)
            if not chunk:
                raise RuntimeError("chat worker closed the connection")
            result += chunk
    print(result.decode("utf-8").strip(), flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"serve", "ping", "message", "restore"}:
        raise SystemExit("usage: session_worker.py serve|ping|message|restore")
    if sys.argv[1] == "serve":
        serve()
    else:
        client(sys.argv[1])

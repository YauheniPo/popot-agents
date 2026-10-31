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
from popot_agents.progress import trace_event, observe_progress, is_progress_message
from popot_agents.skills import prepare_role_config


SOCKET_PATH = os.getenv("HARNESS_SOCKET_PATH", "/workspace/chat.sock")
MAX_HISTORY_BYTES = RUNTIME["limits"]["history_bytes"]


def _trace(event: str, **fields) -> None:
    trace_event(event, **fields)


class Conversation:
    def __init__(self, answer):
        self.answer = answer
        self.messages = []

    def ask(self, message: str, *, timeout_seconds: float | None = None) -> str:
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
            answer = (self.answer(candidate) if timeout_seconds is None else
                      self.answer(candidate, timeout_seconds=timeout_seconds))
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


def answer_with_cli(messages: list[dict[str, str]], instructions: str = "", *,
                    timeout_seconds: float | None = None) -> str:
    if len(messages) == 1:
        prompt = messages[0]["content"]
    else:
        prompt = "\n\n".join(
            f"{item['role'].capitalize()}: {item['content']}" for item in messages
        ) + "\n\nAssistant:"
    if instructions:
        prompt = f"Instructions: {instructions}\n\n{prompt}"
    return (run_harness(prompt) if timeout_seconds is None else
            run_harness(prompt, timeout_seconds=timeout_seconds))


def serve(*, extra_tools: dict | None = None) -> None:
    role_config = prepare_role_config(json.loads(os.getenv("HARNESS_ROLE_JSON", "{}")))
    harness_state = {}
    if os.getenv("HARNESS_SESSION_MODE") == "http":
        answer = lambda messages, **options: run_http(messages, role_config, extra_tools=extra_tools,
                                                      state=harness_state, **options)
    else:
        answer = lambda messages, **options: answer_with_cli(messages, role_config.get("instructions", ""), **options)
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
                    def notify(message):
                        try:
                            self.wfile.write((json.dumps({'progress': message}, ensure_ascii=False) + '\n').encode())
                            self.wfile.flush()
                        except OSError:
                            pass  # A disconnected observer must not stop agent work.
                    with observe_progress(notify if payload.get('progress') is True else None):
                        result = {"answer": conversation.ask(payload.get("message"),
                                  timeout_seconds=payload.get('timeout_seconds'))}
                        if payload.get('result_details') is True:
                            result['outcome'] = harness_state.get('outcome', {
                                'status': 'completed', 'reason': 'answer_returned'})
                            if result['outcome']['status'] in {'partial', 'blocked', 'failed'}:
                                result['checkpoint'] = harness_state
                elif payload.get("action") == "restore":
                    checkpoint = payload.get('checkpoint', {})
                    if not isinstance(checkpoint, dict) or len(json.dumps(checkpoint, ensure_ascii=False).encode()) > MAX_HISTORY_BYTES:
                        raise ValueError('invalid harness checkpoint')
                    conversation.restore(payload.get("messages"))
                    harness_state.clear()
                    harness_state.update(checkpoint)
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
        with connection.makefile('rb') as reader:
            while True:
                line = reader.readline(8 * 1024 * 1024 + 1)
                if not line or not line.endswith(b'\n'):
                    raise RuntimeError("chat worker closed the connection or response is too large")
                result = json.loads(line)
                if isinstance(result, dict) and 'progress' in result:
                    if payload.get('progress') is True and is_progress_message(result['progress']):
                        print(json.dumps(result, ensure_ascii=False), flush=True)
                    continue
                print(json.dumps(result, ensure_ascii=False), flush=True)
                return


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"serve", "ping", "message", "restore"}:
        raise SystemExit("usage: session_worker.py serve|ping|message|restore")
    if sys.argv[1] == "serve":
        serve()
    else:
        client(sys.argv[1])

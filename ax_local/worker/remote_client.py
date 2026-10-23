"""Run one JSON request against the existing Unix-socket chat process in an AX actor."""

import base64
import json
import os
import re
import socket
import sys
from popot_agents.progress import is_progress_message
from popot_agents.runtime_config import RUNTIME

RESPONSE_PREFIX = "POPOT_AX_RESPONSE:"
PROGRESS_PREFIX = "POPOT_AX_PROGRESS:"
MAX_ENCODED_REQUEST_BYTES = 4 * ((RUNTIME['limits']['socket_message_bytes'] + 2) // 3)
REQUEST_DIRECTORY = '/tmp'


def call(encoded: str) -> dict:
    payload = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))
    if not isinstance(payload, dict):
        raise ValueError("request must be a JSON object")
    socket_path = os.getenv("HARNESS_SOCKET_PATH", "/workspace/chat.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(float(os.getenv("HARNESS_TURN_TIMEOUT_SECONDS", "180")) + 10)
        connection.connect(socket_path)
        connection.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        with connection.makefile('rb') as reader:
            while True:
                line = reader.readline(8 * 1024 * 1024 + 1)
                if not line or not line.endswith(b'\n'):
                    raise RuntimeError("AX chat worker closed the connection or response is too large")
                result = json.loads(line)
                if isinstance(result, dict) and 'progress' in result:
                    if payload.get('progress') is True and is_progress_message(result['progress']):
                        print(PROGRESS_PREFIX + json.dumps(result['progress'], ensure_ascii=False), flush=True)
                    continue
                return result


def main(args: list[str]) -> None:
    if len(args) == 1:
        result = call(args[0])
    elif len(args) in {2, 3} and args[0] in {"--chunk", "--consume"}:
        request_id = args[1]
        if not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise ValueError("invalid AX request ID")
        # Transport fragments are temporary, not part of the durable workspace.
        path = os.path.join(REQUEST_DIRECTORY, f'.ax-local-request-{request_id}')
        if args[0] == "--chunk" and len(args) == 3:
            if not re.fullmatch(r"[A-Za-z0-9_=-]{1,48000}", args[2]):
                raise ValueError("invalid AX request chunk")
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags, 0o600)
            try:
                if os.fstat(fd).st_size + len(args[2]) > MAX_ENCODED_REQUEST_BYTES:
                    raise ValueError("AX request is too large")
                os.write(fd, args[2].encode("ascii"))
            finally:
                os.close(fd)
            result = {"status": "ok"}
        elif args[0] == "--consume" and len(args) == 2:
            try:
                fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
                with os.fdopen(fd, encoding="ascii") as file:
                    encoded = file.read(MAX_ENCODED_REQUEST_BYTES + 1)
                if len(encoded) > MAX_ENCODED_REQUEST_BYTES:
                    raise ValueError("AX request is too large")
                result = call(encoded)
            finally:
                os.unlink(path)
        else:
            raise ValueError("invalid AX request command")
    else:
        raise ValueError("invalid AX request command")
    print(RESPONSE_PREFIX + json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])

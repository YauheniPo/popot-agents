"""Keep provider credentials in the AX API container, outside AX Task manifests."""

from __future__ import annotations

import hmac
import json
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error, request

from popot_agents.runtime_config import RUNTIME
from ..config import AX_CONFIG


@dataclass(frozen=True)
class Provider:
    name: str
    model: str
    base_url: str
    api_key: str | None = field(repr=False)


def resolve_provider(name: str, model: str, profiles: dict, environ: dict) -> Provider:
    if not isinstance(name, str) or name not in profiles:
        raise ValueError("AX_LOCAL_PROVIDER must name a configured provider")
    if not isinstance(model, str) or not model.strip() or len(model) > 300:
        raise ValueError("AX_LOCAL_MODEL must name a model")
    profile = profiles[name]
    environment = profile.get("environment", {})
    base_url = environment.get("HARNESS_BASE_URL", "")
    if profile.get("session_mode") != "http" or not base_url.startswith(("http://", "https://")):
        raise ValueError(f"provider {name} has no HTTP chat endpoint")
    key_name = environment.get("HARNESS_API_KEY_ENV")
    api_key = environ.get(key_name) if key_name else None
    if key_name and not api_key:
        raise ValueError(f"provider {name} requires {key_name}")
    return Provider(name, model.strip(), base_url.rstrip("/"), api_key)


def make_upstream_request(selected: Provider, body: bytes) -> request.Request:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("model request must contain JSON") from exc
    if not isinstance(payload, dict) or payload.get("model") != selected.model:
        raise ValueError("model request does not match the selected model")
    headers = {"Content-Type": "application/json"}
    if selected.api_key:
        headers["Authorization"] = f"Bearer {selected.api_key}"
    return request.Request(f"{selected.base_url}/chat/completions", data=body,
                           headers=headers, method="POST")


def network_address_toward(host: str, port: int) -> str:
    """Find this container's source IP on the network toward a known peer."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        connection.connect((host, port))
        return connection.getsockname()[0]


def dispatch_request(selected: Provider, token: str, path: str,
                     authorization: str, body: bytes) -> tuple[int, bytes]:
    if path != "/v1/chat/completions":
        return 404, b'{}'
    if not hmac.compare_digest(authorization, f"Bearer {token}"):
        return 401, b'{}'
    if len(body) < 1 or len(body) > AX_CONFIG["proxy"]["max_request_bytes"]:
        return 413, b'{}'
    try:
        upstream = make_upstream_request(selected, body)
    except ValueError:
        return 400, b'{}'
    # Leave time to deliver an upstream timeout before the worker's socket expires.
    # A shorter remaining turn budget can still disconnect; _reply handles that.
    model_timeout = RUNTIME["timeouts"]["model_request_seconds"]
    timeout = model_timeout - min(AX_CONFIG["proxy"]["response_margin_seconds"], model_timeout / 2)
    started_at = time.monotonic()
    try:
        with request.urlopen(upstream, timeout=timeout) as response:
            answer = response.read(AX_CONFIG["proxy"]["max_response_bytes"] + 1)
            if len(answer) > AX_CONFIG["proxy"]["max_response_bytes"]:
                return 502, b'{}'
            return response.status, answer
    except error.HTTPError as exc:
        return exc.code, b'{}'
    except (error.URLError, TimeoutError, OSError) as exc:
        timed_out = isinstance(exc, TimeoutError) or (
            isinstance(exc, error.URLError) and isinstance(exc.reason, TimeoutError))
        print(json.dumps({
            "event": "model_upstream_timeout" if timed_out else "model_upstream_error",
            "provider": selected.name, "model": selected.model,
            "elapsed_seconds": round(time.monotonic() - started_at, 1),
            "timeout_seconds": timeout,
        }), file=sys.stderr, flush=True)
        return (504 if timed_out else 502), b'{}'


def start_proxy(selected: Provider, token: str, host: str = "0.0.0.0",
                port: int = AX_CONFIG["proxy"]["port"]) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, body: bytes) -> None:
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def do_POST(self) -> None:
            authorization = self.headers.get("Authorization", "")
            if self.path != "/v1/chat/completions" or not hmac.compare_digest(
                    authorization, f"Bearer {token}"):
                status, body = dispatch_request(selected, token, self.path,
                                                authorization, b"")
                self._reply(status, body)
                return
            try:
                size = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._reply(400, b'{}')
                return
            if size < 1 or size > AX_CONFIG["proxy"]["max_request_bytes"]:
                self._reply(413, b'{}')
                return
            body = self.rfile.read(size)
            status, answer = dispatch_request(selected, token, self.path,
                                              authorization, body)
            self._reply(status, answer)

        def log_message(self, _format: str, *_args) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server

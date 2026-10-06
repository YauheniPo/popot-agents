import io
import json
import unittest
from contextlib import redirect_stderr
from urllib.error import URLError
from unittest.mock import Mock, patch

from ax_local.config import AX_CONFIG
from ax_local.api.provider_proxy import dispatch_request, make_upstream_request, resolve_provider, start_proxy
from popot_agents.runtime_config import RUNTIME


class ProviderProxyTests(unittest.TestCase):
    def test_timeout_is_logged_without_secrets_and_returns_gateway_timeout(self):
        selected = resolve_provider('openrouter', 'example/model', self.profiles,
                                    {'OPENROUTER_API_KEY': 'private-key'})
        for failure in (TimeoutError('private-key'), URLError(TimeoutError('private-key'))):
            with self.subTest(failure=type(failure).__name__), \
                 patch('ax_local.api.provider_proxy.request.urlopen', side_effect=failure) as call, \
                 redirect_stderr(io.StringIO()) as logs:
                status, _ = dispatch_request(selected, 'capability', '/v1/chat/completions',
                                             'Bearer capability', b'{"model":"example/model"}')
                self.assertEqual(status, 504)
                self.assertLess(call.call_args.kwargs['timeout'],
                                RUNTIME['timeouts']['model_request_seconds'])
                call.assert_called_once()
                event = json.loads(logs.getvalue())
                self.assertEqual(event['event'], 'model_upstream_timeout')
                self.assertEqual(event['provider'], 'openrouter')
                self.assertNotIn('private-key', logs.getvalue())
                self.assertNotIn('capability', logs.getvalue())

    def test_proxy_handles_disconnected_client_at_headers_or_body(self):
        with patch('ax_local.api.provider_proxy.ThreadingHTTPServer') as server, \
             patch('ax_local.api.provider_proxy.threading.Thread'):
            start_proxy(Mock(), 'capability')
        handler_class = server.call_args.args[1]
        for stage in ('end_headers', 'write'):
            with self.subTest(stage=stage):
                handler = handler_class.__new__(handler_class)
                handler.send_response = Mock()
                handler.send_header = Mock()
                handler.end_headers = Mock()
                handler.wfile = Mock()
                target = handler.end_headers if stage == 'end_headers' else handler.wfile.write
                target.side_effect = BrokenPipeError()
                handler._reply(504, b'{}')
                self.assertTrue(handler.close_connection)

    def setUp(self):
        self.profiles = {
            "openrouter": {
                "session_mode": "http",
                "environment": {
                    "HARNESS_BASE_URL": "https://openrouter.ai/api/v1",
                    "HARNESS_API_KEY_ENV": "OPENROUTER_API_KEY",
                },
            },
            "ollama": {
                "session_mode": "http",
                "environment": {"HARNESS_BASE_URL": "http://host.docker.internal:11434/v1"},
            },
            "nous": {
                "session_mode": "http",
                "environment": {
                    "HARNESS_BASE_URL": "https://inference-api.nousresearch.com/v1",
                    "HARNESS_API_KEY_ENV": "NOUS_API_KEY",
                },
            },
            "nvidia_nim": {
                "session_mode": "http",
                "environment": {
                    "HARNESS_BASE_URL": "https://integrate.api.nvidia.com/v1",
                    "HARNESS_API_KEY_ENV": "NVIDIA_API_KEY",
                },
            },
            "ollama_claude_local": {"session_mode": "cli"},
        }

    def test_resolves_configured_provider_and_model_without_inventing_key(self):
        selected = resolve_provider("openrouter", "example/model", self.profiles,
                                    {"OPENROUTER_API_KEY": "private-key"})
        self.assertEqual(selected.name, "openrouter")
        self.assertEqual(selected.model, "example/model")
        self.assertEqual(selected.base_url, "https://openrouter.ai/api/v1")
        self.assertEqual(selected.api_key, "private-key")
        with self.assertRaises(ValueError):
            resolve_provider("openrouter", "example/model", self.profiles, {})

    def test_proxy_request_uses_selected_upstream_and_key(self):
        selected = resolve_provider("openrouter", "example/model", self.profiles,
                                    {"OPENROUTER_API_KEY": "private-key"})
        body = b'{"model":"example/model","messages":[]}'
        request = make_upstream_request(selected, body)
        self.assertEqual(request.full_url,
                         "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer private-key")
        self.assertEqual(request.data, body)

    def test_proxy_rejects_another_model(self):
        selected = resolve_provider("ollama", "local-model", self.profiles, {})
        with self.assertRaises(ValueError):
            make_upstream_request(selected, b'{"model":"other","messages":[]}')

    def test_other_http_providers_use_their_own_endpoints_and_keys(self):
        for name, key_name, endpoint in (
            ("nous", "NOUS_API_KEY", "https://inference-api.nousresearch.com/v1"),
            ("nvidia_nim", "NVIDIA_API_KEY", "https://integrate.api.nvidia.com/v1"),
        ):
            with self.subTest(provider=name):
                selected = resolve_provider(name, "free-model", self.profiles,
                                            {key_name: "provider-key"})
                self.assertEqual(selected.base_url, endpoint)
                self.assertEqual(selected.api_key, "provider-key")

    def test_cli_profile_is_not_silently_routed_to_ollama(self):
        with self.assertRaisesRegex(ValueError, "no HTTP chat endpoint"):
            resolve_provider("ollama_claude_local", "local-model", self.profiles, {})

    def test_proxy_authenticates_capability_and_keeps_provider_key_upstream(self):
        selected = resolve_provider("openrouter", "example/model", self.profiles,
                                    {"OPENROUTER_API_KEY": "private-key"})

        class UpstreamResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _limit):
                return b'{"choices":[{"message":{"content":"ok"}}]}'

        body = json.dumps({"model": "example/model", "messages": []}).encode()
        with patch("ax_local.api.provider_proxy.request.urlopen", return_value=UpstreamResponse()) as upstream:
            status, _ = dispatch_request(selected, "task-capability", "/v1/chat/completions",
                                         "", body)
            self.assertEqual(status, 401)
            upstream.assert_not_called()

            status, _ = dispatch_request(selected, "task-capability", "/v1/chat/completions",
                                         "Bearer task-capability", body)
            self.assertEqual(status, 200)
            upstream_request = upstream.call_args.args[0]
            self.assertEqual(upstream_request.get_header("Authorization"),
                             "Bearer private-key")

    def test_proxy_enforces_configured_request_limit(self):
        selected = resolve_provider("ollama", "local-model", self.profiles, {})
        with patch.dict(AX_CONFIG["proxy"], {"max_request_bytes": 8}):
            status, _ = dispatch_request(selected, "task-capability", "/v1/chat/completions",
                                         "Bearer task-capability", b"123456789")
        self.assertEqual(status, 413)


if __name__ == "__main__":
    unittest.main()

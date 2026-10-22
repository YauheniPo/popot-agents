"""OmniRoute uses the normal AX profile, capability proxy and tool loop."""

import io
import json
import os
import unittest
from contextlib import redirect_stderr
from urllib import request
from unittest.mock import patch

from ax_local.api.provider_proxy import start_proxy
from ax_local.api.server import make_server
from popot_agents.worker.http_harness import run_http


class OmniRouteTests(unittest.TestCase):
    def test_combo_tool_loop_keeps_gateway_key_out_of_ax_task(self):
        role = {'agent': 'omniroute', 'instructions': 'Use calculate.', 'tools': ['calculate']}
        environment = {
            'AX_LOCAL_WORKER_IMAGE': 'worker@sha256:' + 'a' * 64,
            'AX_LOCAL_BASE_URL': 'http://proxy.test/v1',
            'AX_LOCAL_SESSION_BACKEND': 'file',
            'OMNIROUTE_MODEL': 'ax-free',
            'OMNIROUTE_CODE_MODEL': 'ax-code-free',
            'OMNIROUTE_OPS_MODEL': 'ax-ops-free',
            'OMNIROUTE_API_KEY': 'private-gateway-key',
            'HARNESS2_IMPROVER_MODEL': 'ax-harness-free',
            'OPENROUTER_MODEL': 'direct-provider-model',
            'OPENROUTER_API_KEY': 'direct-provider-key',
            'OLLAMA_CLOUD_MODEL': 'deepseek-v4.1-flash:1017', 'OLLAMA_API_KEY': 'test-ollama-key',
        }
        with patch.dict(os.environ, environment, clear=True), \
             patch('ax_local.api.server.create_server') as create:
            server = make_server()
        for configured_runner in create.call_args.args[0].values():
            self.addCleanup(configured_runner.shutdown_worker_queue)
        runner = create.call_args.args[0]['omniroute']
        self.assertEqual(create.call_args.kwargs['roles']['analyst']['agent'], 'omniroute')
        self.assertEqual(create.call_args.kwargs['roles']['frontend_engineer']['agent'], 'omniroute_code')
        self.assertEqual(create.call_args.args[0]['omniroute_code'].model, 'ax-code-free')
        proxy = start_proxy(server.provider_routes, host='127.0.0.1', port=0)
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        runner.base_url = f'http://127.0.0.1:{proxy.server_port}/v1'
        manifest = runner.task_manifest('0123456789abcdef', role)
        self.assertNotIn('private-gateway-key', json.dumps(manifest))
        worker_env = {item['name']: item['value'] for item in manifest['spec']['env']}
        self.assertNotIn('OMNIROUTE_API_KEY', worker_env)
        self.assertEqual(worker_env['HARNESS_API_KEY_ENV'], 'AX_PROXY_TOKEN')
        upstream_calls = []
        original_urlopen = request.urlopen

        def respond(call, **kwargs):
            if call.full_url == runner.base_url + '/chat/completions':
                self.assertEqual(call.get_header('Authorization'), 'Bearer ' + runner.proxy_token)
                return original_urlopen(call, **kwargs)
            self.assertEqual(call.full_url, 'http://omniroute:20128/v1/chat/completions')
            self.assertEqual(call.get_header('Authorization'), 'Bearer private-gateway-key')
            payload = json.loads(call.data)
            self.assertEqual(payload['model'], 'ax-free')
            upstream_calls.append(payload)
            if len(upstream_calls) == 1:
                self.assertIn('calculate', [t['function']['name'] for t in payload['tools']])
                message = {'content': None, 'tool_calls': [{
                    'id': 'calc', 'type': 'function', 'function': {
                        'name': 'calculate', 'arguments': '{"expression":"2+2"}'}}]}
            else:
                self.assertEqual(payload['messages'][-1]['role'], 'tool')
                self.assertEqual(payload['messages'][-1]['content'], '4')
                message = {'content': '4'}
            response = io.BytesIO(json.dumps({
                'model': 'actual-free-upstream-model', 'choices': [{'message': message}]}).encode())
            response.status = 200
            return response

        with patch.dict(os.environ, worker_env, clear=True), \
             patch('urllib.request.urlopen', side_effect=respond), \
             redirect_stderr(io.StringIO()):
            self.assertEqual(run_http('Calculate 2+2.', role), '4')
        self.assertEqual(len(upstream_calls), 2)


if __name__ == '__main__':
    unittest.main()

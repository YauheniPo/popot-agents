import io
import json
import os
import time
import unittest
from unittest.mock import patch

from ax_local.api.server import kind_control_plane_host, make_server


class AxServerTests(unittest.TestCase):
    def test_role_profiles_select_models_and_share_one_worker_queue(self):
        environment = {
            'AX_LOCAL_WORKER_IMAGE': 'worker@sha256:' + 'a' * 64,
            'AX_LOCAL_BASE_URL': 'http://proxy.test/v1',
            'AX_LOCAL_SESSION_BACKEND': 'file',
            'OPENROUTER_MODEL': 'role-cloud-model',
            'OPENROUTER_API_KEY': 'private-cloud-key',
            'OMNIROUTE_MODEL': 'ax-free',
            'OMNIROUTE_CODE_MODEL': 'ax-code-free',
            'OMNIROUTE_API_KEY': 'private-gateway-key',
            # Stale overrides must not replace the role's profile.
            'AX_LOCAL_PROVIDER': 'openrouter', 'AX_LOCAL_MODEL': 'wrong-model',
            'AX_LOCAL_PROXY_TOKEN': 'old-global-capability',
        }
        with patch.dict(os.environ, environment, clear=True), \
             patch('ax_local.api.server.create_server') as create:
            server = make_server()
        roles = create.call_args.kwargs['roles']
        self.assertEqual(roles['analyst']['agent'], 'omniroute')
        runners = create.call_args.args[0]
        self.assertEqual(set(runners), {'openrouter', 'omniroute', 'omniroute_code'})
        self.assertEqual(runners['openrouter'].model, 'role-cloud-model')
        self.assertEqual(runners['omniroute'].model, 'ax-free')
        self.assertEqual(runners['omniroute_code'].model, 'ax-code-free')
        self.assertIs(runners['openrouter'].worker_queue, runners['omniroute_code'].worker_queue)
        self.assertIs(runners['omniroute'].worker_queue, runners['omniroute_code'].worker_queue)
        self.assertNotEqual(runners['omniroute'].proxy_token, runners['omniroute_code'].proxy_token)
        queue = runners['openrouter'].worker_queue
        leases = [(f'occupied-{index}', queue.acquire(f'occupied-{index}'))
                  for index in range(queue.max_workers)]
        with patch.object(runners['omniroute_code'], '_run') as run:
            with self.assertRaises(TimeoutError):
                runners['omniroute_code'].start_chat('0123456789abcdef', deadline=time.monotonic() + 0.03)
            run.assert_not_called()
        for name, lease in leases:
            queue.release(name, lease)
        self.assertEqual(queue.status()['active'], 0)
        for name, runner in runners.items():
            self.assertEqual(server.provider_routes[runner.proxy_token].name, name)
            self.assertIs(runner.delegation, server.delegation)
            manifest = runner.task_manifest('0123456789abcdef')
            self.assertNotIn('private-cloud-key', json.dumps(manifest))
            self.assertNotIn('private-gateway-key', json.dumps(manifest))
            runner.shutdown_worker_queue()

    def test_missing_used_model_is_reported_without_falling_back_to_another_provider(self):
        environment = {
            'AX_LOCAL_WORKER_IMAGE': 'worker@sha256:' + 'a' * 64,
            'AX_LOCAL_BASE_URL': 'http://proxy.test/v1',
            'AX_LOCAL_SESSION_BACKEND': 'file',
            'OPENROUTER_MODEL': 'cloud-model', 'OPENROUTER_API_KEY': 'test-key',
        }
        with patch.dict(os.environ, environment, clear=True), \
             self.assertRaisesRegex(ValueError, 'omniroute.*OMNIROUTE_MODEL'):
            make_server()

    def test_control_plane_host_follows_ax_context(self):
        self.assertEqual(kind_control_plane_host("kind-popot-ax"),
                         "popot-ax-control-plane")

    def test_roles_keep_tools_and_their_own_agent(self):
        environment = {
            "AX_LOCAL_WORKER_IMAGE": "localhost:5001/popot-agent-ax@sha256:" + "a" * 64,
            "OPENROUTER_MODEL": "selected-model",
            "OPENROUTER_API_KEY": "test-key",
            "OMNIROUTE_MODEL": "ax-free",
            "OMNIROUTE_CODE_MODEL": "ax-code-free",
            "OMNIROUTE_API_KEY": "gateway-key",
            "AX_LOCAL_BASE_URL": "http://172.18.0.5:8004/v1",
            "AX_LOCAL_SESSION_BACKEND": "file",
        }
        with patch.dict(os.environ, environment), patch("ax_local.api.server.create_server") as create:
            server = make_server()
        roles = create.call_args.kwargs["roles"]
        self.assertEqual(roles["backend_engineer"]["agent"], "omniroute_code")
        self.assertIn("bash", roles["backend_engineer"]["tools"])
        self.assertEqual(set(create.call_args.args[0]), {"openrouter", "omniroute", "omniroute_code"})
        self.assertNotIn("allowed_roles", server.delegation.policy)
        self.assertEqual(roles["backend_engineer"]["allowed_roles"], ["qa_engineer", "analyst", "code_reviewer"])
        env = server.delegation.worker_environment("0123456789abcdef", roles["backend_engineer"])
        import json
        self.assertEqual(json.loads(env["AX_DELEGATION_ROLES"]), roles["backend_engineer"]["allowed_roles"])
        descriptions = json.loads(env["AX_DELEGATION_DESCRIPTIONS"])
        self.assertEqual(set(descriptions), set(roles["backend_engineer"]["allowed_roles"]))
        self.assertEqual(descriptions["code_reviewer"], roles["code_reviewer"]["description"])
        self.assertIn("delegate_task", roles["backend_engineer"]["instructions"])

    def test_container_bind_host_is_configurable(self):
        environment = {
            "AX_LOCAL_WORKER_IMAGE": "localhost:5001/popot-agent-ax@sha256:" + "a" * 64,
            "OPENROUTER_MODEL": "selected-model",
            "OPENROUTER_API_KEY": "test-key",
            "OMNIROUTE_MODEL": "ax-free",
            "OMNIROUTE_CODE_MODEL": "ax-code-free",
            "OMNIROUTE_API_KEY": "gateway-key",
            "AX_LOCAL_BASE_URL": "http://172.18.0.5:8004/v1",
            "AX_LOCAL_SESSION_BACKEND": "file",
            "AX_LOCAL_BIND_HOST": "0.0.0.0",
        }
        with patch.dict(os.environ, environment), patch("ax_local.api.server.create_server") as create:
            make_server()
        self.assertEqual(create.call_args.kwargs["host"], "0.0.0.0")

    def test_selected_profile_model_parameters_reach_ax_worker(self):
        from ax_local.api.server import ROOT
        from ax_local.api.provider_proxy import Provider, make_upstream_request
        from popot_agents.worker.http_harness import run_http

        profiles = json.loads((ROOT / 'config/agents.json').read_text())
        profiles['openrouter']['model_parameters'] = {
            'reasoning': {'effort': 'high', 'exclude': True}, 'max_tokens': 4096}
        profiles['nous']['model_parameters'] = {'temperature': 0.1}
        profiles['nvidia_nim']['model_parameters'] = {'chat_template_kwargs': {'enable_thinking': True}}
        for provider in ('openrouter', 'nous', 'nvidia_nim'):
            environment = {
                'AX_LOCAL_WORKER_IMAGE': 'worker@sha256:' + 'a' * 64,
                'OPENROUTER_MODEL': 'test', 'OPENROUTER_API_KEY': 'test-key',
                'NOUS_MODEL': 'test', 'NOUS_API_KEY': 'test-key',
                'NVIDIA_MODEL': 'test', 'NVIDIA_API_KEY': 'test-key',
                'OLLAMA_LOCAL_MODEL': 'local-model',
                'AX_LOCAL_BASE_URL': 'http://model.test/v1',
                'AX_LOCAL_SESSION_BACKEND': 'file',
            }
            with self.subTest(provider=provider), patch.dict(os.environ, environment, clear=True), \
                 patch('ax_local.api.server.Path.read_text', return_value=json.dumps(profiles)), \
                 patch('ax_local.api.server.load_roles', return_value={
                     role: {'agent': provider, 'instructions': '', 'tools': []}
                     for role in ('backend_engineer', 'code_reviewer')}), \
                 patch('ax_local.api.server.create_server') as create:
                make_server()
            runner = create.call_args.args[0][provider]
            try:
                for role in ('backend_engineer', 'code_reviewer'):
                    manifest = runner.task_manifest('0123456789abcdef', {'_ax_role': role})
                    env = {item['name']: item['value'] for item in manifest['spec']['env']}
                    parameters = profiles[provider]['model_parameters']
                    self.assertEqual(json.loads(env['HARNESS_MODEL_PARAMETERS_JSON']), parameters)
                    calls = []

                    def respond(call, timeout):
                        upstream = make_upstream_request(
                            Provider(provider, 'test', 'https://provider.test/v1', None), call.data)
                        calls.append(json.loads(upstream.data))
                        return io.BytesIO(b'{"choices":[{"message":{"content":"ok"}}]}')

                    with patch.dict(os.environ, env, clear=True), \
                         patch('urllib.request.urlopen', side_effect=respond):
                        self.assertEqual(run_http('hello'), 'ok')
                    for key, value in parameters.items():
                        self.assertEqual(calls[0][key], value)
            finally:
                runner.shutdown_worker_queue()


if __name__ == "__main__":
    unittest.main()

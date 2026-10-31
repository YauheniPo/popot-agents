"""Opt-in verification against a disposable, network-isolated OmniRoute image."""

import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import unittest
from urllib import error, request
import uuid

from ax_local.omniroute import Api, ProvisionError, provision


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.environ.get('AX_TEST_OMNIROUTE_LIVE') == '1',
                     'set AX_TEST_OMNIROUTE_LIVE=1 to use disposable Docker containers')
class LiveProvisionTests(unittest.TestCase):
    @classmethod
    def docker(cls, *args, **kwargs):
        result = subprocess.run(['docker', *args], capture_output=True, text=True,
                                timeout=90, **kwargs)
        if result.returncode:
            message = result.stderr[-2000:]
            for value in getattr(cls, 'env', {}).values():
                message = message.replace(value, '[redacted]')
            raise RuntimeError('Disposable Docker check failed: ' + message)
        return result.stdout.strip()

    @classmethod
    def setUpClass(cls):
        from ax_local.cluster.build_versions import BUILD_VERSIONS
        cls.versions = BUILD_VERSIONS
        cls.name = 'popot-omniroute-test-' + uuid.uuid4().hex[:10]
        cls.env = {
            'OMNIROUTE_INITIAL_PASSWORD': secrets.token_hex(24),
            'OMNIROUTE_API_KEY': 'sk-' + secrets.token_hex(24),
            'OPENROUTER_API_KEY': 'sk-test-' + secrets.token_hex(16),
            'NVIDIA_API_KEY': 'nvapi-test-' + secrets.token_hex(16),
            'OLLAMA_API_KEY': 'ollama-test-' + secrets.token_hex(16),
        }
        cls.docker('run', '-d', '--name', cls.name, '--network', 'none', '-e', 'INITIAL_PASSWORD',
                   '-e', 'REQUIRE_API_KEY=true',
                   'diegosouzapw/omniroute:' + BUILD_VERSIONS['OMNIROUTE_VERSION'],
                   env={**os.environ, 'INITIAL_PASSWORD': cls.env['OMNIROUTE_INITIAL_PASSWORD']})
        cls.addClassCleanup(cls.docker, 'rm', '-f', cls.name)
        deadline = time.monotonic() + 90
        while True:
            ready = subprocess.run(['docker', 'exec', cls.name, 'node', 'healthcheck.mjs'],
                                   capture_output=True, timeout=10)
            if ready.returncode == 0:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('Disposable OmniRoute did not become ready')
            time.sleep(1)

    def test_bootstrap_container_repeat_policy_and_failure(self):
        command = ['run', '--rm', '--network', 'container:' + self.name,
                   '-e', 'OMNIROUTE_URL=http://127.0.0.1:20128']
        for name in self.env:
            command += ['-e', name]
        command += ['-v', str(ROOT / 'ax_local/omniroute.py') + ':/bootstrap/omniroute.py:ro',
                    '-v', str(ROOT / 'config') + ':/config:ro',
                    self.versions['PYTHON_IMAGE'], 'python', '/bootstrap/omniroute.py',
                    '--config', '/config/omniroute/settings.json',
                    '--combos', '/config/omniroute/combos.json']
        output = self.docker(*command, env={**os.environ, **self.env})
        self.assertIn('configuration verified', output)
        for secret in self.env.values():
            self.assertNotIn(secret, output)
        self.docker(*command, env={**os.environ, **self.env})
        check = ['run', '--rm', '--network', 'container:' + self.name, '-w', '/project',
                 '-e', '_AX_OMNIROUTE_INNER=1', '-e', 'PYTHONDONTWRITEBYTECODE=1']
        for name in self.env:
            check += ['-e', name]
        for path in ['ax_local/omniroute.py', 'ax_local/tests/test_omniroute_provision_live.py']:
            check += ['-v', str(ROOT / path) + ':/project/' + path + ':ro']
        check += ['-v', str(ROOT / 'config') + ':/project/config:ro',
                  self.versions['PYTHON_IMAGE'], 'python', '-m', 'unittest',
                  'ax_local.tests.test_omniroute_provision_live.InnerProvisionTests']
        self.docker(*check, env={**os.environ, **self.env})


@unittest.skipUnless(os.environ.get('_AX_OMNIROUTE_INNER') == '1',
                     'executed inside the disposable gateway network namespace')
class InnerProvisionTests(unittest.TestCase):
    def test_credential_redaction_preserves_diff_and_tool_calls(self):
        api = Api('http://127.0.0.1:20128')
        api.request('POST', '/api/auth/login', {'password': os.environ['OMNIROUTE_INITIAL_PASSWORD']})
        fake_token = 'ghp_' + 'a' * 36
        diff = '@@ -1 +1 @@\n-return left - right\n+return left + right\n'
        arguments = json.dumps({'path': 'review.py', 'line': 2})
        payload = {'model': 'ax-code-free', 'messages': [
            {'role': 'user', 'content': 'Review this diff:\n' + diff + '\nFixture token: ' + fake_token},
            {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'review-read-1', 'type': 'function', 'function': {
                    'name': 'read_file', 'arguments': arguments}}]},
            {'role': 'tool', 'tool_call_id': 'review-read-1', 'content': 'Fixture result token: ' + fake_token},
        ]}
        result = api.request('POST', '/api/guardrails/test', {'input': payload})
        self.assertFalse(result['blocked'])
        redacted = result['payload']
        self.assertNotIn(fake_token, json.dumps(redacted))
        self.assertIn('[REDACTED:', json.dumps(redacted))
        self.assertEqual(redacted['model'], payload['model'])
        self.assertIn(diff, redacted['messages'][0]['content'])
        self.assertEqual(redacted['messages'][1], payload['messages'][1])
        self.assertEqual(redacted['messages'][2]['tool_call_id'], 'review-read-1')

    def test_repeat_policy_and_failure(self):
        env = dict(os.environ)
        api = Api('http://127.0.0.1:20128')
        config = json.loads((ROOT / 'config/omniroute/settings.json').read_text())
        combos = json.loads((ROOT / 'config/omniroute/combos.json').read_text())
        provision(api, config, combos, env)
        before = api.request('GET', '/api/settings/export-json')
        provision(api, config, combos, env)
        after = api.request('GET', '/api/settings/export-json')
        for field in ('providerConnections', 'combos', 'apiKeys'):
            self.assertEqual([x['id'] for x in before[field]], [x['id'] for x in after[field]])
        self.assertEqual(len(after['providerConnections']), len(config['providers']))
        self.assertEqual(len(after['combos']), len(combos))
        self.assertEqual(len(after['apiKeys']), 1)
        key = after['apiKeys'][0]
        self.assertEqual(key['modelAccessMode'], 'restricted')
        self.assertFalse(key['compressionEnabled'])
        self.assertFalse(key['allowAutoCombos'])
        self.assertEqual(key['cacheDefaultMode'], 'bypass')
        self.assertEqual(set(key['allowedCombos']), set(combos))
        self.assertEqual(set(key['allowedConnections']),
                         {c['id'] for c in after['providerConnections']})
        for combo in after['combos']:
            expected = combos[combo['name']]
            self.assertEqual([x['model'] for x in combo['models']],
                             [x['model'] for x in expected['models']])
            for name, value in expected['config'].items():
                self.assertEqual(combo['config'][name], value)
        catalog = request.Request('http://127.0.0.1:20128/v1/models',
                                  headers={'Authorization': 'Bearer ' + env['OMNIROUTE_API_KEY']})
        with request.urlopen(catalog, timeout=10) as response:
            self.assertEqual(response.status, 200)
        call = request.Request('http://127.0.0.1:20128/v1/chat/completions',
                               headers={'Authorization': 'Bearer ' + env['OMNIROUTE_API_KEY'],
                                        'Content-Type': 'application/json'},
                               data=json.dumps({'model': 'openrouter/openai/gpt-4o',
                                                'messages': [{'role': 'user', 'content': 'test'}]}).encode())
        with self.assertRaises(error.HTTPError) as denied:
            request.urlopen(call, timeout=10)
        self.assertEqual(denied.exception.code, 403)
        denied.exception.close()
        with self.assertRaisesRegex(ProvisionError, 'does not match'):
            provision(api, config, combos,
                      {**env, 'OMNIROUTE_API_KEY': 'different-private-secret'})


if __name__ == '__main__':
    unittest.main()

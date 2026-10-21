"""Provisioning must preserve fallback order, secret boundaries and key policy."""

import copy
import hashlib
import io
import json
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from ax_local.omniroute import Api, ProvisionError, provision
from popot_agents.runtime_config import RUNTIME


class ManagedConfigTests(unittest.TestCase):
    def test_cooldown_budget_leaves_time_for_model_fallback(self):
        root = Path(__file__).resolve().parents[2]
        config = json.loads((root / 'config/omniroute/settings.json').read_text())
        combos = json.loads((root / 'config/omniroute/combos.json').read_text())
        wait = config['resilience']['comboCooldownWait']
        connection_wait = config['resilience']['waitForCooldown']
        connection_wait_ms = connection_wait['maxRetries'] * connection_wait['maxRetryWaitSec'] * 1000
        for name, combo in combos.items():
            with self.subTest(combo=name):
                budget = combo['config']
                self.assertLess(budget['comboTimeoutMs'], RUNTIME['timeouts']['model_request_seconds'] * 1000)
                if len(combo['models']) > 1:
                    self.assertLessEqual(max(wait['budgetMs'], connection_wait_ms) + budget['targetTimeoutMs'],
                                         budget['comboTimeoutMs'])


class Gateway:
    def __init__(self):
        self.state = {'settings': {'unmanaged': True}, 'providerConnections': [],
                      'combos': [], 'apiKeys': []}
        self.compression = {'enabled': True, 'defaultMode': 'light'}
        self.resilience = {'providerCooldown': {'enabled': False}}
        self.writes = []
        self.fail_policy = False

    def request(self, method, path, body=None):
        if path == '/api/auth/login':
            return {'success': True}
        if method == 'GET':
            return copy.deepcopy({
                '/api/settings/export-json': self.state,
                '/api/settings/compression': self.compression,
                '/api/resilience': self.resilience,
            }[path])
        self.writes.append((method, path, copy.deepcopy(body)))
        if path == '/api/settings/compression':
            self.compression.update(body)
        elif path == '/api/resilience':
            self.resilience.update(body)
        elif path == '/api/providers':
            conn = {**body, 'id': f'connection-{len(self.state["providerConnections"])}'}
            self.state['providerConnections'].append(conn)
            return {'connection': conn}
        elif path.startswith('/api/providers/'):
            next(c for c in self.state['providerConnections']
                 if c['id'] == path.split('/')[-1]).update(body)
        elif path == '/api/combos':
            combo = {**body, 'id': f'combo-{len(self.state["combos"])}'}
            for i, model in enumerate(combo['models']):
                model['id'] = f'server-step-{i}'
            self.state['combos'].append(combo)
            return combo
        elif path.startswith('/api/combos/'):
            next(c for c in self.state['combos'] if c['id'] == path.split('/')[-1]).update(body)
        elif path == '/api/settings/import-json':
            self.state['settings'].update(body.get('settings', {}))
            self.state['apiKeys'].extend(copy.deepcopy(body.get('apiKeys', [])))
        elif path.startswith('/api/keys/'):
            if self.fail_policy:
                raise ProvisionError('PATCH /api/keys failed (HTTP 500)')
            next(k for k in self.state['apiKeys'] if k['id'] == path.split('/')[-1]).update(body)
        else:
            raise AssertionError((method, path))
        return {}


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.gateway = Gateway()
        self.env = {'OMNIROUTE_INITIAL_PASSWORD': 'test-admin-secret',
                    'OMNIROUTE_API_KEY': 'test-gateway-secret',
                    'OPENROUTER_API_KEY': 'test-upstream-secret',
                    'NVIDIA_API_KEY': 'test-other-upstream-secret'}
        self.config = {
            'settings': {'autoRefreshProviderQuota': True, 'autoRefreshProviderQuotaInterval': 180},
            'compression': {'enabled': False, 'defaultMode': 'off'},
            'resilience': {'connectionCooldown': {'apikey': {'useUpstreamRetryHints': True}}},
            'providers': {
                'openrouter': {'name': 'popot-openrouter', 'apiKeyEnv': 'OPENROUTER_API_KEY'},
                'nvidia': {'name': 'popot-nvidia', 'apiKeyEnv': 'NVIDIA_API_KEY'},
            },
            'apiKey': {'name': 'popot-ax-free', 'keyEnv': 'OMNIROUTE_API_KEY',
                       'permissions': {'compressionEnabled': False, 'cacheDefaultMode': 'bypass',
                                       'allowAutoCombos': False, 'scopes': ['self:usage']}},
        }
        self.combos = {'ax-free': {
            'name': 'ax-free', 'strategy': 'priority',
            'models': [{'kind': 'model', 'providerId': 'openrouter', 'model': 'openrouter/code:free'},
                       {'kind': 'model', 'providerId': 'nvidia', 'model': 'nvidia/backup'}],
            'config': {'targetTimeoutMs': 45000, 'comboTimeoutMs': 105000,
                       'maxGlobalAttempts': 3, 'maxSetRetries': 0},
        }}

    def apply(self):
        return provision(self.gateway, self.config, self.combos, self.env)

    def test_fresh_gateway_and_repeated_apply_preserve_order_policy_and_ids(self):
        self.apply()
        state = self.gateway.state
        combo = state['combos'][0]
        self.assertEqual([m['model'] for m in combo['models']],
                         ['openrouter/code:free', 'nvidia/backup'])
        self.assertEqual(combo['config'], self.combos['ax-free']['config'])
        self.assertEqual([m['connectionId'] for m in combo['models']],
                         ['connection-0', 'connection-1'])
        key = state['apiKeys'][0]
        self.assertEqual(key['modelAccessMode'], 'restricted')
        self.assertEqual(key['allowedModels'], ['openrouter/code:free', 'nvidia/backup'])
        self.assertEqual(key['allowedCombos'], ['ax-free'])
        self.assertEqual(key['allowedConnections'], ['connection-0', 'connection-1'])
        self.assertFalse(key['allowAutoCombos'])
        self.assertFalse(key['compressionEnabled'])
        self.assertEqual(key['cacheDefaultMode'], 'bypass')
        before = copy.deepcopy(state)
        self.gateway.writes.clear()
        self.apply()
        self.assertEqual(self.gateway.state, before)
        self.assertEqual(self.gateway.writes, [])

    def test_config_changes_update_fallbacks_and_key_allowlist_without_duplicates(self):
        self.apply()
        self.combos['ax-free']['models'].reverse()
        self.combos['ax-free']['models'][0]['model'] = 'nvidia/new-backup'
        self.apply()
        self.assertEqual(len(self.gateway.state['combos']), 1)
        self.assertEqual(self.gateway.state['combos'][0]['models'][0]['model'], 'nvidia/new-backup')
        self.assertEqual(self.gateway.state['apiKeys'][0]['allowedModels'],
                         ['nvidia/new-backup', 'openrouter/code:free'])

    def test_unmanaged_objects_and_settings_are_preserved(self):
        self.gateway.state['providerConnections'].append(
            {'id': 'personal', 'name': 'personal', 'provider': 'openrouter', 'apiKey': 'private'})
        self.gateway.state['combos'].append({'id': 'personal', 'name': 'personal', 'models': []})
        self.gateway.state['apiKeys'].append({'id': 'personal', 'name': 'personal', 'key': 'private'})
        original = copy.deepcopy(self.gateway.state)
        self.apply()
        for field in ('providerConnections', 'combos', 'apiKeys'):
            self.assertEqual(self.gateway.state[field][0], original[field][0])
        self.assertTrue(self.gateway.state['settings']['unmanaged'])

    def test_existing_account_with_same_secret_is_reused_without_renaming(self):
        self.gateway.state['providerConnections'].append({
            'id': 'manual-openrouter', 'name': 'My OpenRouter', 'provider': 'openrouter',
            'apiKey': self.env['OPENROUTER_API_KEY']})
        self.apply()
        self.apply()
        self.assertEqual(len(self.gateway.state['providerConnections']), 2)
        self.assertEqual(self.gateway.state['providerConnections'][0]['name'], 'My OpenRouter')
        self.assertEqual(self.gateway.state['combos'][0]['models'][0]['connectionId'],
                         'manual-openrouter')

    def test_missing_secret_and_invalid_provider_reference_fail_before_mutation(self):
        del self.env['NVIDIA_API_KEY']
        with self.assertRaisesRegex(ProvisionError, 'NVIDIA_API_KEY'):
            self.apply()
        self.assertEqual(self.gateway.writes, [])
        self.env['NVIDIA_API_KEY'] = 'restored'
        self.combos['ax-free']['models'][0]['providerId'] = 'missing'
        with self.assertRaises(ProvisionError):
            self.apply()
        self.assertEqual(self.gateway.writes, [])

    def test_existing_gateway_key_mismatch_fails_before_mutation(self):
        self.gateway.state['apiKeys'].append(
            {'id': 'old', 'name': 'popot-ax-free', 'key': 'different-secret'})
        with self.assertRaisesRegex(ProvisionError, 'does not match'):
            self.apply()
        self.assertEqual(self.gateway.writes, [])

    def test_disabled_gateway_key_is_not_reactivated(self):
        self.gateway.state['apiKeys'].append({
            'id': 'old', 'name': 'popot-ax-free', 'key': self.env['OMNIROUTE_API_KEY'],
            'isActive': False})
        with self.assertRaisesRegex(ProvisionError, 'inactive'):
            self.apply()
        self.assertEqual(self.gateway.writes, [])

    def test_hashed_gateway_key_is_reused(self):
        self.gateway.state['apiKeys'].append({
            'id': 'existing', 'name': 'popot-ax-free', 'key': 'masked',
            'keyHash': hashlib.sha256(self.env['OMNIROUTE_API_KEY'].encode()).hexdigest()})
        self.apply()
        self.assertEqual(len(self.gateway.state['apiKeys']), 1)
        self.assertEqual(self.gateway.state['apiKeys'][0]['id'], 'existing')

    def test_policy_failure_leaves_new_key_restricted_and_retry_finishes(self):
        self.gateway.fail_policy = True
        with self.assertRaises(ProvisionError):
            self.apply()
        key = self.gateway.state['apiKeys'][0]
        self.assertEqual(key['modelAccessMode'], 'restricted')
        self.assertEqual(key['allowedModels'], [])
        self.gateway.fail_policy = False
        self.apply()
        self.assertEqual(len(self.gateway.state['apiKeys']), 1)
        self.assertFalse(key['allowAutoCombos'])

    def test_server_ignoring_a_policy_field_is_a_failed_provision(self):
        original = self.gateway.request

        def ignore(method, path, body=None):
            if method == 'PATCH' and path.startswith('/api/keys/'):
                body = {**body, 'allowAutoCombos': True}
            return original(method, path, body)

        with patch.object(self.gateway, 'request', side_effect=ignore):
            with self.assertRaisesRegex(ProvisionError, 'verification'):
                self.apply()

    def test_secrets_are_not_printed(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.apply()
        for secret in self.env.values():
            self.assertNotIn(secret, output.getvalue())


class ApiTests(unittest.TestCase):
    def test_login_cookie_is_kept_and_credential_posts_are_not_redirected(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', '0')))
                seen.append(self.path)
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', '/unexpected')
                else:
                    self.send_response(200)
                    self.send_header('Set-Cookie', 'auth_token=test-session; Path=/')
                self.end_headers()
                self.wfile.write(b'{}')

            def do_GET(self):
                seen.append(self.path)
                self.send_response(200 if self.headers.get('Cookie') == 'auth_token=test-session' else 401)
                self.end_headers()
                self.wfile.write(b'{"authenticated":true}')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        api = Api(f'http://127.0.0.1:{server.server_port}')
        api.request('POST', '/api/auth/login', {'password': 'test-secret'})
        self.assertTrue(api.request('GET', '/api/settings')['authenticated'])
        with self.assertRaisesRegex(ProvisionError, 'HTTP 302'):
            api.request('POST', '/redirect', {'apiKey': 'test-secret'})
        self.assertNotIn('/unexpected', seen)

    def test_http_errors_do_not_echo_a_server_response_containing_credentials(self):
        api = Api('http://localhost:20128')
        error = HTTPError('http://localhost:20128/api/providers', 400, 'bad', {},
                          io.BytesIO(b'{"error":"echoed-private-secret"}'))
        with patch.object(api.opener, 'open', side_effect=error):
            with self.assertRaises(ProvisionError) as result:
                api.request('POST', '/api/providers', {'apiKey': 'private-secret'})
        self.assertNotIn('private-secret', str(result.exception))
        self.assertIn('HTTP 400', str(result.exception))


if __name__ == '__main__':
    unittest.main()

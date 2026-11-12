"""Deployment-owned Phoenix providers, catalogs and request defaults."""
import asyncio
import copy
import importlib
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]


class FakeAPI:
    def __init__(self):
        self.providers = {'unmanaged': {'id': 'keep', 'name': 'unmanaged'}}
        self.writes = []

    def query(self, query, variables=None):
        if 'mutation' not in query:
            return {'generativeModelCustomProviders': {
                'edges': [{'node': p} for p in self.providers.values()],
                'pageInfo': {'hasNextPage': False, 'endCursor': None}}}
        payload = copy.deepcopy(variables['input'])
        self.writes.append(payload)
        config = payload['clientConfig']['openai']
        item = {'id': payload.get('id', 'id-' + payload['name']),
                'name': payload['name'], 'provider': payload['provider'],
                'modelNames': ['test/model'],
                'config': {'__typename': 'OpenAICustomProviderConfig',
                           'openaiApiType': config['openaiApiType'],
                           'openaiClientKwargs': config['openaiClientKwargs']}}
        self.providers[payload['name']] = item
        operation = ('patchGenerativeModelCustomProvider' if 'id' in payload
                     else 'createGenerativeModelCustomProvider')
        return {operation: {'provider': item}}


class PhoenixProviderTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module('evals.phoenix_providers')
        self.settings = [{'name': 'Test', 'provider': 'test',
                          'base_url': 'https://test.example/v1',
                          'api_key_env': 'TEST_API_KEY', 'models': ['test/model']}]

    def test_configuration_declares_real_models_and_nous_defaults(self):
        settings = self.module.load_settings()
        self.assertEqual({p['name'] for p in settings},
                         {'OpenRouter', 'NVIDIA NIM', 'Ollama Cloud', 'Nous Research'})
        nous = next(p for p in settings if p['name'] == 'Nous Research')
        self.assertIn('user=phoenix-local', nous['extra_body']['tags'])
        self.assertTrue(all(p['models'] and p['api_key_env'] for p in settings))

    def test_redeploy_preserves_ids_rotates_keys_and_leaves_other_providers_alone(self):
        api = FakeAPI()
        first = self.module.provision(self.settings, {'TEST_API_KEY': 'private-first'}, api)
        saved_id = api.providers['Test']['id']
        second = self.module.provision(self.settings, {'TEST_API_KEY': 'private-rotated'}, api)
        self.assertEqual(api.providers['Test']['id'], saved_id)
        self.assertEqual(set(api.providers), {'Test', 'unmanaged'})
        self.assertEqual(api.providers['unmanaged'], {'id': 'keep', 'name': 'unmanaged'})
        self.assertEqual(api.writes[-1]['clientConfig']['openai']
                         ['openaiAuthenticationMethod']['apiKey'], 'private-rotated')
        self.assertNotIn('private-', json.dumps([first, second]))

    def test_missing_optional_provider_key_skips_its_registration(self):
        api = FakeAPI()
        result = self.module.provision(self.settings, {}, api)
        self.assertFalse(api.writes)
        self.assertEqual(result, [{'name': 'Test', 'status': 'skipped_missing_key'}])

    def test_bad_catalog_readback_fails_deployment(self):
        api = FakeAPI()
        query = api.query

        def mismatched(document, variables=None):
            result = query(document, variables)
            if 'mutation' not in document and 'Test' in api.providers:
                api.providers['Test']['modelNames'] = ['gpt-wrong-provider']
            return result

        with patch.object(api, 'query', side_effect=mismatched):
            with self.assertRaisesRegex(RuntimeError, 'verification'):
                self.module.provision(self.settings, {'TEST_API_KEY': 'private'}, api)

    def test_invalid_public_configuration_is_rejected(self):
        import tempfile
        for change in ({'base_url': 'https://user:secret@example.com/v1'},
                       {'models': []}, {'models': ['same', 'same']},
                       {'api_key_env': 'a secret value'}, {'extra_body': []}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'config.json'
                path.write_text(json.dumps({'x-providers': [{**self.settings[0], **change}]}))
                with self.assertRaises(ValueError):
                    self.module.load_settings(path)

    def test_graphql_errors_never_echo_submitted_keys(self):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"errors":[{"message":"private-key"}]}'
        api = self.module.API('http://localhost:6006', 'private-key')
        with patch.object(api.opener, 'open', return_value=response):
            with self.assertRaises(RuntimeError) as raised:
                api.query('query { test }')
        self.assertNotIn('private-key', str(raised.exception))

    def test_request_defaults_stay_on_the_selected_client_and_allow_explicit_options(self):
        async def run():
            first, second = AsyncMock(), AsyncMock()
            nous = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=first)))
            other = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=second)))
            defaults = {'tags': ['product=phoenix', 'user=phoenix-local']}
            self.module.configure_client(nous, defaults)
            self.module.configure_client(other, {})
            await asyncio.gather(
                nous.chat.completions.create(model='nous', extra_body={'temperature': 0}),
                other.chat.completions.create(model='other'))
            self.assertEqual(first.call_args.kwargs['extra_body'], {**defaults, 'temperature': 0})
            self.assertNotIn('extra_body', second.call_args.kwargs)
            await nous.chat.completions.create(extra_body={'tags': ['user=explicit']})
            self.assertEqual(first.call_args.kwargs['extra_body'], {'tags': ['user=explicit']})
            self.assertEqual(defaults, {'tags': ['product=phoenix', 'user=phoenix-local']})
        asyncio.run(run())

    def test_normal_launch_includes_phoenix_bootstrap(self):
        self.assertIn('evals/up.sh', (ROOT / 'ax_local/up.sh').read_text())
        self.assertIn('phoenix-init', (ROOT / 'evals/up.sh').read_text())


if __name__ == '__main__':
    unittest.main()

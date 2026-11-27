"""Opt-in fresh Phoenix deployment and small real provider requests."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import unittest
from urllib import error, request
import uuid

from ax_local.cluster.build_versions import BUILD_VERSIONS
from evals.phoenix_providers import API, load_settings, provision, read_providers


@unittest.skipUnless(os.environ.get('PHOENIX_TEST_PROVIDERS_LIVE'),
                     'requires Docker and provider keys; makes small real model requests')
class PhoenixProviderDeploymentTests(unittest.TestCase):
    def test_fresh_deploy_redeploy_and_all_four_providers(self):
        from popot_agents.orchestrator.main import load_dotenv
        root = Path(__file__).resolve().parents[1]
        load_dotenv(root / '.env')
        settings = load_settings()
        for item in settings:
            self.assertTrue(os.environ.get(item['api_key_env']), item['api_key_env'])
        container = 'popot-phoenix-check-' + uuid.uuid4().hex[:8]
        environment = {**os.environ,
            'PHOENIX_SECRET': secrets.token_urlsafe(40),
            'PHOENIX_ADMIN_SECRET': secrets.token_urlsafe(40),
            'PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD': secrets.token_urlsafe(40)}
        command = ['docker', 'run', '--rm', '-d', '--name', container,
            '-p', '127.0.0.1::6006', '--mount', 'type=tmpfs,destination=/data',
            '-v', str(root / 'evals') + ':/opt/popot/evals:ro']
        for name in ('PHOENIX_SECRET', 'PHOENIX_ADMIN_SECRET', 'PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD'):
            command += ['-e', name]
        command += ['-e', 'PHOENIX_ENABLE_AUTH=true', '-e', 'PHOENIX_WORKING_DIR=/data',
            '-e', 'PHOENIX_TELEMETRY_ENABLED=false', BUILD_VERSIONS['PHOENIX_IMAGE'],
            '/opt/popot/evals/phoenix_server.py']
        self.addCleanup(lambda: subprocess.run(['docker', 'rm', '-f', container], capture_output=True))
        subprocess.run(command, env=environment, check=True, capture_output=True)
        port = subprocess.check_output(['docker', 'port', container, '6006/tcp'], text=True).strip().rsplit(':', 1)[1]
        base = 'http://127.0.0.1:' + port
        for _ in range(90):
            try:
                with request.urlopen(base + '/healthz', timeout=2):
                    break
            except OSError:
                state = subprocess.run(['docker', 'inspect', container, '--format', '{{.State.Running}}'],
                                       capture_output=True, text=True)
                self.assertEqual(state.stdout.strip(), 'true', 'Phoenix stopped before becoming ready')
                time.sleep(1)
        else:
            self.fail('Phoenix did not become ready')
        api = API(base, environment['PHOENIX_ADMIN_SECRET'])
        provision(settings, os.environ, api)
        original_ids = {name: item['id'] for name, item in read_providers(api).items()}
        self.assertEqual(len(original_ids), 4)
        provision(settings, os.environ, api)
        self.assertEqual(original_ids, {name: item['id'] for name, item in read_providers(api).items()})
        print('Fresh deployment and redeployment catalogs/IDs: passed', flush=True)
        for item in settings:
            with self.subTest(provider=item['name']):
                # No manual extra_body: the deployed Nous integration must supply it.
                body = {'model': 'custom:' + original_ids[item['name']] + ':' + item['models'][0],
                    'messages': [{'role': 'user', 'content': 'Reply OK.'}],
                    'max_tokens': 8, 'stream': False}
                call = request.Request(base + '/v1/chat/completions',
                    data=json.dumps(body).encode(), headers={'Content-Type': 'application/json',
                    'Authorization': 'Bearer ' + environment['PHOENIX_ADMIN_SECRET']})
                try:
                    with request.urlopen(call, timeout=60) as response:
                        completion = json.load(response)
                except error.HTTPError as exc:
                    details = exc.read().decode()
                    for key in [environment['PHOENIX_ADMIN_SECRET'],
                                *(os.environ[p['api_key_env']] for p in settings)]:
                        details = details.replace(key, '[REDACTED]')
                    self.fail(f'Phoenix HTTP {exc.code}: {details[:700]}')
                self.assertTrue(completion.get('choices'))
                print(item['name'] + ' via Phoenix: passed', flush=True)

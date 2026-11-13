"""Exercise template deletion during a deterministic golden warmup window."""

import json
import os
from pathlib import Path
import subprocess
import time
import unittest
import uuid

from ax_local.config import AX_CONFIG


@unittest.skipUnless(os.getenv('AX_TEST_TEMPLATE_CLEANUP_LIVE'), 'requires local AX/Substrate')
class LiveTemplateCleanupTests(unittest.TestCase):
    def test_warmup_templates_do_not_leave_running_golden_workers(self):
        local = Path(__file__).resolve().parents[1] / '.local'
        ate = [str(local / 'bin/kubectl-ate'), '--kubeconfig=' + str(local / 'kubeconfig'),
               '--context=' + AX_CONFIG['context']]

        def run(*args, **kwargs):
            return subprocess.run([*ate, *args], capture_output=True, text=True, check=True,
                                  timeout=60, **kwargs).stdout

        def inspect(kind, atespace):
            return json.loads(run('get', kind, '--atespace=' + atespace, '-o', 'json'))

        golden_uids, template_uids = [], []
        for iteration in range(3):
            name = 'popot-cleanup-fixture-' + uuid.uuid4().hex[:16]
            # No readyz probe gives the golden reconciler a 20-second warmup.
            # No model, provider credentials, user files or active task is used.
            manifest = {
                'metadata': {'name': name, 'atespace': AX_CONFIG['atespace']},
                'containers': [{'name': 'guest', 'image': (local / 'worker-image').read_text().strip(),
                                'command': ['python', '-c', 'import time; time.sleep(600)']}],
                'snapshotsConfig': {'storageLocation': 'gs://ate-snapshots/popot-ax/',
                                    'onPause': 'SNAPSHOT_CONTENT_SCOPE_DATA',
                                    'onCommit': 'SNAPSHOT_CONTENT_SCOPE_DATA'},
                'sandboxConfig': {'sandboxClass': 'SANDBOX_CLASS_GVISOR', 'configName': 'gvisor-default'},
            }
            run('create', 'actor-template', '-f', '-', input=json.dumps(manifest))
            try:
                templates = inspect('actor-template', AX_CONFIG['atespace']).get('actorTemplates', [])
                uid = next(t['metadata']['uid'] for t in templates if t['metadata']['name'] == name)
                template_uids.append(uid)
                deadline = time.monotonic() + 45
                golden = None
                while time.monotonic() < deadline:
                    golden = next((a for a in inspect('actors', 'ate-golden').get('actors', [])
                                   if a['metadata']['name'] == uid), None)
                    if golden and golden.get('status', {}).get('state') == 'ACTOR_STATE_RUNNING':
                        break
                    time.sleep(.25)
                self.assertIsNotNone(golden, 'golden reconciler did not create the fixture')
                self.assertEqual(golden['status']['state'], 'ACTOR_STATE_RUNNING')
                golden_uids.append(golden['metadata']['uid'])
                started = time.monotonic()
                run('delete', 'actor-template', name, '--atespace=' + AX_CONFIG['atespace'])
                actors = inspect('actors', 'ate-golden').get('actors', [])
                self.assertFalse(any(a['metadata']['name'] == uid for a in actors), 'golden worker survived deletion')
                print(f'Golden warmup cleanup {iteration + 1}/3: {time.monotonic() - started:.2f}s', flush=True)
            finally:
                templates = inspect('actor-template', AX_CONFIG['atespace']).get('actorTemplates', [])
                if any(t['metadata']['name'] == name for t in templates):
                    run('delete', 'actor-template', name, '--atespace=' + AX_CONFIG['atespace'])
        # Cover a queued reconciliation and more than one resync interval.
        time.sleep(25)
        actors = inspect('actors', 'ate-golden').get('actors', [])
        self.assertFalse(any(a['metadata']['name'] in template_uids for a in actors))
        result = subprocess.run(['docker', 'exec', 'popot-ax-control-plane', 'ps', '-eo', 'args'],
                                capture_output=True, text=True, check=True, timeout=15)
        self.assertFalse(any(uid in result.stdout for uid in golden_uids), 'physical sandbox still running')

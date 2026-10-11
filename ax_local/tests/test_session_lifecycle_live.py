"""Opt-in lifecycle check against local AX/Substrate, without any model calls."""

import base64
import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ax_local.api.ax_runner import AxAgentRunner
from ax_local.api import ax_runner
from ax_local.config import AX_CONFIG
from popot_agents.orchestrator.main import create_server
from tests import test_chat as chat_tests


class LifecycleRunner(AxAgentRunner):
    def task_manifest(self, chat_id, role_config=None):
        manifest = super().task_manifest(chat_id, role_config)
        # Use the production conversation server with a deterministic answer.
        # A workspace marker distinguishes snapshot restore from a new worker.
        script = '''
from pathlib import Path
import popot_agents.worker.session_worker as worker
from ax_local.worker.entrypoint import main
def answer(messages, *args, **kwargs):
    marker = Path('/workspace/lifecycle-marker')
    previous = marker.read_text() if marker.exists() else 'fresh'
    marker.write_text('preserved')
    artifact = Path('/workspace/lifecycle-artifact.bin')
    if not artifact.exists():
        artifact.write_bytes(bytes(range(256)) * 4096)
    return previous + '|' + '|'.join(m['content'] for m in messages if m['role'] == 'user')
worker.run_http = answer
main()
'''
        # Keep multiline code out of the Task's YAML command serialization.
        encoded = base64.b64encode(script.encode()).decode()
        manifest['spec']['command'] = [
            'python', '-u', '-c', "import base64; exec(base64.b64decode('" + encoded + "'))"]
        return manifest


@unittest.skipUnless(os.getenv('AX_TEST_LIVE'), 'requires local AX/Substrate and worker image')
class LiveSessionLifecycleTests(unittest.TestCase):
    def test_suspend_continuation_and_expiry_restore_context(self):
        root = Path(__file__).resolve().parents[2]
        image = os.getenv('AX_TEST_WORKER_IMAGE') or (
            root / 'ax_local/.local/worker-image').read_text().strip()
        environment = {'PATH': str(root / 'ax_local/.local/bin') + os.pathsep + os.environ['PATH'],
                       'KUBECONFIG': str(root / 'ax_local/.local/kubeconfig')}
        mode = os.getenv('AX_TEST_SNAPSHOT_MODE', AX_CONFIG['snapshot_mode'])
        self.assertIn(mode, ('data', 'full'))
        with patch.dict(os.environ, environment), patch.dict(AX_CONFIG, {'snapshot_mode': mode}), \
             tempfile.TemporaryDirectory() as directory:
            runner = LifecycleRunner(image=image, model='unused', base_url='http://localhost:1/v1')
            server = create_server({'test': runner}, port=0, session_dir=directory)
            try:
                def post(payload):
                    return chat_tests.TaskSessionTests._post_to_handler(
                        server.RequestHandlerClass, server, payload)

                status, first = post({'agent': 'test', 'message': 'remember 137'})
                self.assertEqual(status, 200, first)
                self.assertEqual(first['answer'], 'fresh|remember 137')
                sid = first['sessionId']
                original = server.live[sid].worker
                self.assertEqual(original.status, 'suspended')
                self.assertEqual(runner.task_status(original.container_name)[0], 'Suspended')

                expected = 'remember 137'
                for turn in range(3):
                    message = 'continue ' + str(turn)
                    expected += '|' + message
                    with patch.object(ax_runner, 'emit_progress', wraps=ax_runner.emit_progress) as progress:
                        status, second = post({'sessionId': sid, 'message': message})
                    self.assertEqual(status, 200, second)
                    resumed = server.live[sid].worker
                    self.assertIs(resumed, original, 'runtime could not restore the native snapshot')
                    self.assertEqual(second['answer'], 'preserved|' + expected)
                    self.assertEqual(resumed.status, 'suspended')
                    events = [event for event in progress.call_args_list
                              if event.args[0] in ('snapshot_saved', 'snapshot_restored')]
                    self.assertEqual([event.args[0] for event in events],
                                     ['snapshot_restored', 'snapshot_saved'])
                    for event in events:
                        self.assertEqual(event.kwargs['mode'], mode)
                        print('AX live snapshot:', event.args[0], event.kwargs)
                # Check binary files too; a transcript-only fallback cannot pass.
                resumed.resume()
                result = runner._run('ssh', resumed.container_name, '--', 'sha256sum',
                                     '/workspace/lifecycle-artifact.bin')
                self.assertIn(hashlib.sha256(bytes(range(256)) * 4096).hexdigest(), result.stdout)
                # Exercise real chunk transport beyond its former 200 KB limit.
                committed = list(resumed._messages)
                resumed.restore([{'role': 'user', 'content': 'я' * 190000},
                                 {'role': 'assistant', 'content': 'готово'}])
                resumed.restore(committed)
                resumed.after_turn()

                # Advance the idle age; no two-hour wall-clock wait is needed.
                server.live[sid].last_used = time.monotonic() - 7201
                server.service_actions()
                self.assertTrue(resumed.closed)
                self.assertNotIn(sid, server.live)
                self.assertIsNotNone(server.store.get(sid))

                status, third = post({'sessionId': sid, 'message': 'after expiry'})
                self.assertEqual(status, 200, third)
                self.assertIsNot(server.live[sid].worker, resumed)
                self.assertEqual(third['answer'], 'fresh|' + expected + '|after expiry')
                self.assertEqual(server.live[sid].worker.status, 'suspended')
            finally:
                server.server_close()


if __name__ == '__main__':
    unittest.main()

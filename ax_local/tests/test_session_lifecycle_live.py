"""Opt-in lifecycle check against local AX/Substrate, without any model calls."""

import base64
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ax_local.api.ax_runner import AxAgentRunner
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
def answer(messages, *args, **kwargs):
    marker = Path('/workspace/lifecycle-marker')
    previous = marker.read_text() if marker.exists() else 'fresh'
    marker.write_text('preserved')
    return previous + '|' + '|'.join(m['content'] for m in messages if m['role'] == 'user')
worker.run_http = answer
worker.serve()
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
        image = (root / 'ax_local/.local/worker-image').read_text().strip()
        environment = {'PATH': str(root / 'ax_local/.local/bin') + os.pathsep + os.environ['PATH'],
                       'KUBECONFIG': str(root / 'ax_local/.local/kubeconfig')}
        with patch.dict(os.environ, environment), tempfile.TemporaryDirectory() as directory:
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

                status, second = post({'sessionId': sid, 'message': 'continue'})
                self.assertEqual(status, 200, second)
                resumed = server.live[sid].worker
                if resumed is original:
                    self.assertEqual(second['answer'], 'preserved|remember 137|continue')
                    print('AX live continuation: native snapshot restored')
                else:
                    self.assertTrue(original.closed)
                    self.assertEqual(second['answer'], 'fresh|remember 137|continue')
                    print('AX live continuation: failed snapshot replaced; full transcript restored')
                if os.getenv('AX_REQUIRE_NATIVE_RESUME'):
                    self.assertIs(resumed, original, 'runtime could not restore the native snapshot')
                self.assertEqual(resumed.status, 'suspended')

                # Advance the idle age; no two-hour wall-clock wait is needed.
                server.live[sid].last_used = time.monotonic() - 7201
                server.service_actions()
                self.assertTrue(resumed.closed)
                self.assertNotIn(sid, server.live)
                self.assertIsNotNone(server.store.get(sid))

                status, third = post({'sessionId': sid, 'message': 'after expiry'})
                self.assertEqual(status, 200, third)
                self.assertIsNot(server.live[sid].worker, resumed)
                self.assertEqual(third['answer'], 'fresh|remember 137|continue|after expiry')
                self.assertEqual(server.live[sid].worker.status, 'suspended')
            finally:
                server.server_close()


if __name__ == '__main__':
    unittest.main()

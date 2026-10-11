"""Opt-in SDK round trip through two real AX sandboxes, without model calls.

Run in the local API container with AX_TEST_SDK_LIVE=1 and its kubeconfig/image
mounts. The child uses the production conversation server with a fixed answer.
"""

import base64
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid

from ax_local.api.ax_runner import AxAgentRunner
from ax_local.api.delegation import DelegationService, start_a2a_server
from ax_local.api.provider_proxy import network_address_toward
from ax_local.api.server import kind_control_plane_host
from ax_local.config import AX_CONFIG
from popot_agents.orchestrator.session_store import SessionStore


class SDKLiveRunner(AxAgentRunner):
    def task_manifest(self, chat_id, role_config=None):
        manifest = super().task_manifest(chat_id, role_config)
        script = '''
import popot_agents.worker.session_worker as worker
worker.run_http = lambda *args, **kwargs: 'sdk-live-result'
worker.serve()
'''
        encoded = base64.b64encode(script.encode()).decode()
        manifest['spec']['command'] = ['python', '-u', '-c',
            "import base64; exec(base64.b64decode('" + encoded + "'))"]
        return manifest


@unittest.skipUnless(os.getenv('AX_TEST_SDK_LIVE'), 'requires local API container and AX sandboxes')
class LiveA2ASDKTests(unittest.TestCase):
    def test_sandbox_sdk_client_delegates_to_real_child_and_cleans_it_up(self):
        host = network_address_toward(kind_control_plane_host(AX_CONFIG['context']), 6443)
        image = Path(os.environ['AX_LOCAL_WORKER_IMAGE_FILE']).read_text().strip()
        runner = SDKLiveRunner(image=image, model='unused', base_url='http://localhost:1/v1')
        roles = {name: {'agent': 'test', 'instructions': name, 'tools': [], '_ax_role': name}
                 for name in ('parent', 'child')}
        roles['parent']['allowed_roles'] = ['child']
        policy = {'max_depth': 2, 'max_calls_per_turn': 2, 'turn_timeout_seconds': 90,
                  'poll_interval_seconds': 0.1}
        parent_id = uuid.uuid4().hex[:16]
        parent_name = 'popot-chat-' + parent_id
        with tempfile.TemporaryDirectory() as directory:
            service = DelegationService(roles, policy, {config['agent']: runner for config in roles.values()}, SessionStore(directory),
                                        f'http://{host}:0')
            runner.delegation = service
            server = start_a2a_server(service, host, 0)
            manifest = runner.task_manifest(parent_id, roles['parent'])
            script = '''
import json, time
from pathlib import Path
from ax_local.worker.delegation import make_tools
output = make_tools()['delegate_task']['call'](
    {'role': 'child', 'task': 'deterministic SDK check'}, timeout_seconds=70, call_id='live-sdk')
Path('/workspace/a2a-result.json').write_text(output)
time.sleep(120)
'''
            encoded = base64.b64encode(script.encode()).decode()
            manifest['spec']['command'] = ['python', '-u', '-c',
                "import base64; exec(base64.b64decode('" + encoded + "'))"]
            result = None
            try:
                with service.turn(parent_id, 90):
                    runner._run('apply', '-f', '-', input=json.dumps(runner.workspace_manifest()))
                    runner._run('apply', '-f', '-', input=json.dumps(manifest))
                    deadline = time.monotonic() + 80
                    while time.monotonic() < deadline:
                        read = runner._run('ssh', parent_name, '--', 'cat',
                                           '/workspace/a2a-result.json', allow_failure=True, timeout=5)
                        if read.returncode == 0:
                            result = json.loads(read.stdout)
                            break
                        time.sleep(0.5)
                    self.assertIsNotNone(result, 'sandbox did not return an SDK result')
                    self.assertEqual(result.get('state'), 'TASK_STATE_COMPLETED', result)
                    self.assertEqual(result['answer'], 'sdk-live-result')
                    self.assertEqual(runner.task_status('popot-chat-' + result['sessionId'])[0], None)
                    self.assertEqual(runner.worker_queue.status()['active'], 0)
                print('A2A SDK: real parent sandbox -> SDK server -> child sandbox; result and cleanup confirmed')
            finally:
                runner.shutdown_worker_queue()
                service.close()
                server.server_close()
                runner._run('delete', 'task', parent_name, allow_failure=True)
                runner.maintenance()


if __name__ == '__main__':
    unittest.main()

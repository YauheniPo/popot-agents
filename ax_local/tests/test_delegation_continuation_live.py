"""Opt-in DATA snapshot continuation with production harness and deterministic model replies."""

import base64
import json
import os
import tempfile
import unittest
import uuid

from ax_local.api.ax_runner import AxAgentRunner
from ax_local.api.delegation import DelegationService, start_a2a_server
from ax_local.api.provider_proxy import network_address_toward
from ax_local.api.server import kind_control_plane_host
from ax_local.config import AX_CONFIG
from ax_local.worker.delegation import make_tools
from popot_agents.orchestrator.session_store import SessionStore


class ContinuationRunner(AxAgentRunner):
    def remote(self, *args, **kwargs):
        result = super().remote(*args, **kwargs)
        if result.get('error'):
            print('Deterministic fixture error:', result['error'], flush=True)
        return result

    def task_manifest(self, chat_id, role_config=None):
        manifest = super().task_manifest(chat_id, role_config)
        script = '''
import io, json
from popot_agents.worker import http_harness
from ax_local.worker.entrypoint import main
def respond(request, **kwargs):
    body = json.loads(request.data)
    users = [m['content'] for m in body['messages'] if m['role'] == 'user']
    if users[-1] == 'finish':
        if not any(m.get('tool_call_id') == 'write-proof' for m in body['messages']):
            raise RuntimeError('fixture tool evidence was not restored')
        if body['messages'][-1].get('tool_call_id') == 'read-proof':
            if 'preserved' not in body['messages'][-1]['content']:
                raise RuntimeError('fixture workspace marker was not restored')
            report = {'status': 'completed', 'answer': 'workspace and tool evidence restored',
                      'evidence': ['marker preserved', 'write-proof tool result restored'],
                      'remaining_work': [], 'errors': []}
            message = {'content': json.dumps(report)}
        else:
            message = {'tool_calls': [{'id': 'read-proof', 'type': 'function', 'function': {
                'name': 'read_file', 'arguments': json.dumps({'path': 'continuation.txt'})}}]}
    elif body['messages'][-1].get('tool_call_id') == 'write-proof':
        if body.get('tool_choice') != 'none':
            raise TimeoutError('deterministic post-write model timeout')
        report = {'status': 'partial', 'answer': 'file created; verification remains',
                  'evidence': ['write-proof'], 'remaining_work': ['verify file'], 'errors': []}
        message = {'content': json.dumps(report)}
    else:
        message = {'tool_calls': [{'id': 'write-proof', 'type': 'function', 'function': {
            'name': 'write_file', 'arguments': json.dumps({'path': 'continuation.txt', 'content': 'preserved'})}}]}
    return io.BytesIO(json.dumps({'choices': [{'message': {'role': 'assistant', **message}}]}).encode())
http_harness.request.urlopen = respond
main()
'''
        encoded = base64.b64encode(script.encode()).decode()
        manifest['spec']['command'] = ['python', '-u', '-c',
            "import base64; exec(base64.b64decode('" + encoded + "'))"]
        return manifest


@unittest.skipUnless(os.getenv('AX_TEST_CONTINUATION_LIVE'), 'requires AX container, kubeconfig and new worker image')
class LiveDelegationContinuationTests(unittest.TestCase):
    def test_partial_child_restores_same_data_snapshot_and_tool_history(self):
        host = network_address_toward(kind_control_plane_host(AX_CONFIG['context']), 6443)
        runner = ContinuationRunner(image=os.environ['AX_TEST_WORKER_IMAGE'], model='fixture',
                                    base_url='http://localhost:1/v1')
        self.assertEqual(runner.snapshot_mode, 'data')
        roles = {name: {'agent': 'fixture', '_ax_role': name, 'instructions': name,
                        'tools': ['write_file', 'read_file'], 'max_tool_rounds': 2}
                 for name in ('parent', 'child')}
        roles['parent']['allowed_roles'] = ['child']
        policy = {'max_depth': 2, 'max_calls_per_turn': 3, 'turn_timeout_seconds': 180,
                  'poll_interval_seconds': 0.1}
        parent_id = uuid.uuid4().hex[:16]
        with tempfile.TemporaryDirectory() as directory:
            service = DelegationService(roles, policy, {'fixture': runner}, SessionStore(directory), f'http://{host}:0')
            runner.delegation = service
            server = start_a2a_server(service, host, 0)
            environment = service.worker_environment(parent_id, roles['parent'])
            environment['AX_DELEGATION_POLL_SECONDS'] = '0.1'
            delegate = make_tools(environment)['delegate_task']['call']
            try:
                with service.turn(parent_id, 180):
                    first = json.loads(delegate({'role': 'child', 'task': 'start'},
                                                timeout_seconds=90, call_id='live-start'))
                    self.assertEqual(first.get('outcome', {}).get('status'), 'partial', first)
                    self.assertTrue(first['resumable'])
                    task_name = 'popot-chat-' + first['sessionId']
                    self.assertEqual(runner.task_status(task_name)[0], 'Suspended')
                    second = json.loads(delegate({'role': 'child', 'session_id': first['sessionId'], 'task': 'finish'},
                                                 timeout_seconds=90, call_id='live-continue'))
                    self.assertEqual(second.get('outcome', {}).get('status'), 'completed', second)
                    self.assertEqual(second['sessionId'], first['sessionId'])
                    self.assertEqual(second['answer'], 'workspace and tool evidence restored')
                    self.assertEqual(len(service.store.get(first['sessionId'])['messages']), 4)
                    self.assertEqual(service.grants[parent_id].run.calls, 2)
                    self.assertEqual(runner.task_status(task_name)[0], None)
                    self.assertEqual(runner.worker_queue.status()['active'], 0)
            finally:
                service.close()
                server.server_close()
                runner.shutdown_worker_queue()
                runner.maintenance()


if __name__ == '__main__':
    unittest.main()

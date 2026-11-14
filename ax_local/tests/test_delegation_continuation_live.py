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


SCREENSHOT_SCRIPT = '''from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 400, "height": 300})
    page.set_content("<canvas width=320 height=240></canvas>")
    page.evaluate("() => { const ctx = document.querySelector('canvas').getContext('2d'); const image = ctx.createImageData(320, 240); for (let i = 0; i < image.data.length; i++) image.data[i] = i % 4 === 3 ? 255 : Math.floor(Math.random() * 256); ctx.putImageData(image, 0, 0); }")
    page.screenshot(path="artifacts/screenshot.png")
    browser.close()
'''


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
http_harness.RUNTIME['model']['request_retries'] = 0
real_urlopen = http_harness.request.urlopen
def respond(request, **kwargs):
    if not request.full_url.endswith('/chat/completions'):
        return real_urlopen(request, **kwargs)
    body = json.loads(request.data)
    users = [m['content'] for m in body['messages'] if m['role'] == 'user']
    if users[-1] == 'artifact':
        if not any(m.get('tool_call_id') == 'context7-docs' for m in body['messages']):
            if body['messages'][-1].get('tool_call_id') == 'context7-resolve':
                if '/pytest-dev/pytest' not in body['messages'][-1]['content']:
                    raise RuntimeError('Context7 library was not resolved')
                name = 'mcp__context7__query-docs'
                arguments = {'libraryId': '/pytest-dev/pytest/9.2.0',
                             'query': 'How to parametrize test functions?', 'language': 'Python'}
                call_id = 'context7-docs'
            else:
                name = 'mcp__context7__resolve-library-id'
                arguments = {'libraryName': 'pytest', 'query': 'pytest 9.2 parametrized tests'}
                call_id = 'context7-resolve'
            message = {'tool_calls': [{'id': call_id, 'type': 'function', 'function': {
                'name': name, 'arguments': json.dumps(arguments)}}]}
            return io.BytesIO(json.dumps({'choices': [{'message': {'role': 'assistant', **message}}]}).encode())
        if body['messages'][-1].get('tool_call_id') == 'context7-docs' and 'parametrize' not in body['messages'][-1]['content']:
            raise RuntimeError('Context7 documentation was not returned')
        if body['messages'][-1].get('tool_call_id') == 'write-screenshot':
            message = {'content': json.dumps({'status': 'completed', 'answer': 'patch prepared',
                'evidence': ['patch written'], 'remaining_work': [], 'errors': []})}
        else:
            screenshot = __SCREENSHOT_SCRIPT__
            message = {'tool_calls': [{'id': 'write-artifact', 'type': 'function', 'function': {
                'name': 'write_file', 'arguments': json.dumps({'path': 'artifacts/fix.patch',
                    'content': 'unpublished patch'})}},
                {'id': 'write-screenshot', 'type': 'function', 'function': {
                    'name': 'bash', 'arguments': json.dumps({'command': "python - <<'PY'\\n" + screenshot + "PY"})}}]}
    elif users[-1] == 'finish':
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
        script = script.replace('__SCREENSHOT_SCRIPT__', repr(SCREENSHOT_SCRIPT))
        encoded = base64.b64encode(script.encode()).decode()
        manifest['spec']['command'] = ['python', '-u', '-c',
            "import base64; exec(base64.b64decode('" + encoded + "'))"]
        return manifest


@unittest.skipUnless(os.getenv('AX_TEST_CONTINUATION_LIVE'), 'requires AX container, kubeconfig and new worker image')
class LiveDelegationContinuationTests(unittest.TestCase):
    def test_completed_child_patch_survives_sandbox_removal_and_service_restart(self):
        import hashlib
        import subprocess
        import sys
        host = network_address_toward(kind_control_plane_host(AX_CONFIG['context']), 6443)
        runner = ContinuationRunner(image=os.environ['AX_TEST_WORKER_IMAGE'], model='fixture',
                                    base_url='http://localhost:1/v1')
        roles = {name: {'agent': 'fixture', '_ax_role': name, 'instructions': name,
                        'tools': ['write_file', 'bash'], 'max_tool_rounds': 5,
                        'permissions': {'workspace': 'persistent', 'shell': True, 'internet': True}}
                 for name in ('parent', 'child')}
        roles['parent']['allowed_roles'] = ['child']
        roles['child']['mcpServers'] = {'context7': {'url': 'https://mcp.context7.com/mcp',
            'optional': True, 'tools': ['resolve-library-id', 'query-docs']}}
        policy = {'max_depth': 2, 'max_calls_per_turn': 3, 'turn_timeout_seconds': 180,
                  'poll_interval_seconds': .1}
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(directory)
            service = DelegationService(roles, policy, {'fixture': runner}, store, f'http://{host}:0')
            runner.delegation = service
            server = start_a2a_server(service, host, 0)
            environment = service.worker_environment(uuid.uuid4().hex[:16], roles['parent'])
            delegate = make_tools(environment)['delegate_task']['call']
            try:
                parent_id = next(sid for sid, grant in service.grants.items() if grant.role == 'parent')
                with service.turn(parent_id, 120):
                    result = json.loads(delegate({'role': 'child', 'task': 'artifact'},
                        timeout_seconds=90, call_id='live-artifact'))
                self.assertEqual(result['outcome']['status'], 'completed', result)
                expected = result['outcome']['artifacts']
                self.assertEqual(expected[0], {'name': 'fix.patch', 'content': 'unpublished patch',
                    'sha256': hashlib.sha256(b'unpublished patch').hexdigest()})
                self.assertEqual(len(expected), 2)
                self.assertEqual(expected[1]['name'], 'screenshot.png')
                self.assertEqual(expected[1]['encoding'], 'base64')
                png = base64.b64decode(expected[1]['content'], validate=True)
                self.assertTrue(png.startswith(b'\x89PNG\r\n\x1a\n'))
                from popot_agents.runtime_config import RUNTIME
                self.assertGreater(len(png), RUNTIME['limits']['tool_output_chars'])
                self.assertEqual(expected[1]['sha256'], hashlib.sha256(png).hexdigest())
                self.assertIsNone(runner.task_status('popot-chat-' + result['sessionId'])[0])
                service.close()
                script = '''
import json,sys,threading
from urllib import request
from popot_agents.orchestrator.main import create_server
server=create_server({'fixture':object()},roles={},session_dir=sys.argv[1],host='127.0.0.1',port=0)
thread=threading.Thread(target=server.serve_forever)
thread.start()
try:
    with request.urlopen(f'http://127.0.0.1:{server.server_port}/chats/{sys.argv[2]}') as response:
        print(json.dumps(json.load(response)['artifacts']))
finally:
    server.shutdown();thread.join();server.server_close()
'''
                restored = subprocess.run([sys.executable, '-c', script, directory, result['sessionId']],
                                          capture_output=True, text=True, timeout=10)
                self.assertEqual(restored.returncode, 0, restored.stderr)
                self.assertEqual(json.loads(restored.stdout), expected)
            finally:
                service.close()
                server.server_close()
                runner.shutdown_worker_queue()
                runner.maintenance()

    def test_unfinished_child_restores_same_data_snapshot_and_tool_history(self):
        host = network_address_toward(kind_control_plane_host(AX_CONFIG['context']), 6443)
        runner = ContinuationRunner(image=os.environ['AX_TEST_WORKER_IMAGE'], model='fixture',
                                    base_url='http://localhost:1/v1')
        self.assertEqual(runner.snapshot_mode, 'data')
        roles = {name: {'agent': 'fixture', '_ax_role': name, 'instructions': name,
                        'tools': ['write_file', 'read_file'], 'max_tool_rounds': 2}
                 for name in ('parent', 'child')}
        roles['parent']['allowed_roles'] = ['child']
        roles['child']['coordination'] = {'max_turns': 1}
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
                    self.assertEqual(first.get('outcome', {}).get('status'), 'blocked', first)
                    self.assertEqual(first['outcome']['reason'], 'workflow_turn_limit')
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

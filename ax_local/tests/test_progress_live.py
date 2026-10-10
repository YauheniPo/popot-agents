"""Opt-in real AX progress transport without model calls."""
import base64
import json
import os
from pathlib import Path
import unittest
import uuid
import tempfile

from ax_local.api.ax_runner import AxAgentRunner
from popot_agents.progress import observe_progress
from popot_agents.orchestrator.session_store import SessionStore
from ax_local.api.delegation import DelegationService, start_a2a_server
from ax_local.api.provider_proxy import network_address_toward


class ProgressLiveRunner(AxAgentRunner):
    def task_manifest(self, chat_id, role_config=None):
        manifest = super().task_manifest(chat_id, role_config)
        script = '''
import time
import popot_agents.worker.session_worker as worker
from popot_agents.worker.http_harness import _trace
def answer(*args, **kwargs):
    _trace('model_request', round=1)
    _trace('tool_call', name='read_file', arguments={'path': 'app.py', 'token': 'PRIVATE'})
    time.sleep(.5)
    _trace('tool_call', name='bash', arguments={'command': 'git status', 'token': 'SECRET'})
    return 'progress-live-result'
worker.run_http = answer
worker.serve()
'''
        encoded = base64.b64encode(script.encode()).decode()
        manifest['spec']['command'] = ['python', '-u', '-c', "import base64; exec(base64.b64decode('"+encoded+"'))"]
        return manifest


class DelegatedProgressLiveRunner(AxAgentRunner):
    def task_manifest(self, chat_id, role_config=None):
        manifest = super().task_manifest(chat_id, role_config)
        script = '''
import json, os, time
import popot_agents.worker.session_worker as worker
from popot_agents.worker.http_harness import _trace
from ax_local.worker.delegation import make_tools
role = json.loads(os.environ['HARNESS_ROLE_JSON'])['_ax_role']
tools = make_tools()
def answer(*args, **kwargs):
    _trace('model_request', round=1)
    if role == 'backend_engineer':
        _trace('tool_call', name='delegate_task')
        result = json.loads(tools['delegate_task']['call'](
            {'role': 'code_reviewer', 'task': 'deterministic child'},
            timeout_seconds=40, call_id='live-progress'))
        if result.get('isError'): raise RuntimeError('child failed')
        return result['answer']
    _trace('assistant_message', content='Reviewing the diff now.')
    _trace('assistant_reasoning', reasoning='Provider reasoning fixture.')
    _trace('future_harness_event', detail={'file': 'app.py', 'stage': 'review'})
    _trace('tool_call', name='mcp__github__pull_request_read', arguments={'token': 'PRIVATE'})
    time.sleep(1)
    _trace('tool_call', name='read_file')
    return 'delegated-progress-live-result'
worker.run_http = answer
worker.serve(extra_tools=tools)
'''
        encoded = base64.b64encode(script.encode()).decode()
        manifest['spec']['command'] = ['python', '-u', '-c', "import base64; exec(base64.b64decode('"+encoded+"'))"]
        return manifest


class ReasoningLiveRunner(AxAgentRunner):
    def task_manifest(self, chat_id, role_config=None):
        manifest = super().task_manifest(chat_id, role_config)
        script = '''
import io, json
import popot_agents.worker.session_worker as worker
import popot_agents.worker.http_harness as harness
def respond(call, timeout):
    body = json.loads(call.data)
    assert body['reasoning'] == {'effort': 'high', 'exclude': False}, 'reasoning was not forwarded'
    return io.BytesIO(json.dumps({'choices': [{'message': {
        'role': 'assistant', 'content': 'reasoning-live-result',
        'reasoning': 'Provider reasoning fixture.',
        'reasoning_details': [{'type': 'reasoning.summary', 'summary': 'Provider summary fixture.'}]
    }}]}).encode())
harness.request.urlopen = respond
worker.serve()
'''
        encoded = base64.b64encode(script.encode()).decode()
        manifest['spec']['command'] = ['python', '-u', '-c', "import base64; exec(base64.b64decode('"+encoded+"'))"]
        return manifest


@unittest.skipUnless(os.getenv('AX_TEST_PROGRESS_LIVE'), 'requires local API container and AX sandbox')
class LiveAXProgressTests(unittest.TestCase):
    def test_reasoning_configuration_and_provider_output_in_real_sandbox(self):
        runner = ReasoningLiveRunner(
            image=Path(os.environ['AX_LOCAL_WORKER_IMAGE_FILE']).read_text().strip(),
            model='unused', base_url='http://localhost:1/v1',
            model_parameters={'reasoning': {'effort': 'high', 'exclude': False}})
        messages, chat = [], None
        try:
            with observe_progress(messages.append):
                chat = runner.start_chat(uuid.uuid4().hex[:16])
                result = chat.send('deterministic reasoning check', timeout_seconds=30)
            self.assertEqual(result['answer'], 'reasoning-live-result')
            events = [json.loads(message) for message in messages]
            expected = {'event': 'assistant_reasoning', 'round': 1,
                        'reasoning': 'Provider reasoning fixture.',
                        'reasoning_details': [{'type': 'reasoning.summary', 'summary': 'Provider summary fixture.'}]}
            self.assertIn(expected, events)
            answer_index = next(i for i, event in enumerate(events) if event['event'] == 'assistant_answer')
            self.assertLess(events.index(expected), answer_index)
        finally:
            if chat: chat.close()
            runner.shutdown_worker_queue()
            runner.maintenance()

    def test_parent_and_child_sandboxes_forward_role_progress(self):
        host = network_address_toward('popot-ax-control-plane', 6443)
        runner = DelegatedProgressLiveRunner(
            image=Path(os.environ['AX_LOCAL_WORKER_IMAGE_FILE']).read_text().strip(),
            model='unused', base_url='http://localhost:1/v1')
        roles = {role: {'agent': 'test', '_ax_role': role, 'instructions': role,
                        'tools': [], 'timeout_seconds': 60}
                 for role in ['backend_engineer', 'code_reviewer']}
        roles['backend_engineer']['allowed_roles'] = ['code_reviewer']
        policy = {'max_depth': 2, 'max_calls_per_turn': 4, 'turn_timeout_seconds': 60,
                  'poll_interval_seconds': .1}
        with tempfile.TemporaryDirectory() as directory:
            service = DelegationService(roles, policy, {config['agent']: runner for config in roles.values()}, SessionStore(directory), 'http://' + host)
            runner.delegation = service
            server = start_a2a_server(service, host, 0)
            messages, chat = [], None
            try:
                with observe_progress(messages.append):
                    chat = runner.start_chat(uuid.uuid4().hex[:16], roles['backend_engineer'])
                    result = chat.send('deterministic parent', timeout_seconds=60)
                self.assertEqual(result['answer'], 'delegated-progress-live-result')
                events = [json.loads(m) for m in messages]
                self.assertTrue(any(e.get('agent_role') == 'code_reviewer' and e.get('name') == 'mcp__github__pull_request_read' for e in events))
                self.assertTrue(any(e.get('agent_role') == 'code_reviewer' and e.get('name') == 'read_file' for e in events))
                self.assertIn({'event': 'assistant_message', 'content': 'Reviewing the diff now.',
                               'agent_role': 'code_reviewer'}, events)
                self.assertIn({'event': 'assistant_reasoning', 'reasoning': 'Provider reasoning fixture.',
                               'agent_role': 'code_reviewer'}, events)
                self.assertIn({'event': 'future_harness_event', 'detail': {'file': 'app.py', 'stage': 'review'},
                               'agent_role': 'code_reviewer'}, events)
                self.assertEqual(sum(e.get('agent_role') == 'code_reviewer' and e.get('name') == 'mcp__github__pull_request_read' for e in events), 1)
                self.assertNotIn('PRIVATE', json.dumps(messages))
                print('AX live delegated activity:', ' → '.join(messages))
            finally:
                if chat: chat.close()
                service.close()
                server.server_close()
                runner.shutdown_worker_queue()
                runner.maintenance()

    def test_real_sandbox_reports_before_answer_and_cleans_up(self):
        runner = ProgressLiveRunner(image=Path(os.environ['AX_LOCAL_WORKER_IMAGE_FILE']).read_text().strip(),
                                    model='unused', base_url='http://localhost:1/v1')
        messages = []
        chat = None
        try:
            with observe_progress(messages.append):
                chat = runner.start_chat(uuid.uuid4().hex[:16])
                result = chat.send('deterministic check', timeout_seconds=30)
            self.assertEqual(result['answer'], 'progress-live-result')
            events = [json.loads(m) for m in messages]
            self.assertTrue(any(e['event'] == 'model_request' for e in events))
            self.assertTrue(any(e.get('name') == 'read_file' for e in events))
            self.assertTrue(any(e.get('name') == 'bash' for e in events))
            self.assertNotIn('PRIVATE', json.dumps(messages))
            self.assertNotIn('SECRET', json.dumps(messages))
            print('AX live short activity:', ' → '.join(messages))
        finally:
            if chat: chat.close()
            runner.shutdown_worker_queue()
            runner.maintenance()


if __name__ == '__main__':
    unittest.main()

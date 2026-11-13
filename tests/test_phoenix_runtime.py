"""Runtime traces preserve ownership and survive receiver outages."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('trace delivery did not finish')
        time.sleep(.01)


@contextmanager
def receiver():
    state = {'spans': [], 'fail': False, 'requests': 0, 'lose_ack': False}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['requests'] += 1
            if state['fail']:
                self.send_response(503)
                self.end_headers()
                return
            span = payload['data'][0]
            if any(s['context']['span_id'] == span['context']['span_id'] for s in state['spans']):
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({'detail': json.dumps({'total_received': 1,
                    'total_duplicates': 1, 'total_invalid': 0,
                    'duplicate_spans': [span['context']]})}).encode())
                return
            state['spans'].extend(payload['data'])
            if state['lose_ack']:
                self.close_connection = True
                return
            self.send_response(202)
            self.end_headers()
            self.wfile.write(json.dumps({'total_received': len(payload['data']),
                                        'total_queued': len(payload['data'])}).encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class PhoenixRuntimeTests(unittest.TestCase):
    def settings(self, directory, endpoint):
        return {'enabled': True, 'project': 'popot-runtime', 'endpoint': endpoint,
                'outbox_directory': str(directory), 'request_timeout_seconds': 1,
                'retry_seconds': .05, 'api_key_env': 'PHOENIX_ADMIN_SECRET'}

    def test_config_rejects_credentials_in_urls_and_invalid_or_missing_settings(self):
        from ax_local.api.phoenix import load_config

        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(tmp, 'http://localhost:6006')
            path = Path(tmp) / 'config.json'
            path.write_text(json.dumps({'x-tracing': settings}))
            self.assertEqual(load_config(path), settings)
            for key, value in [('enabled', 'true'), ('project', 'bad/name'),
                               ('endpoint', 'http://user:password@localhost:6006'),
                               ('endpoint', None), ('outbox_directory', 'relative'),
                               ('retry_seconds', float('nan')), ('request_timeout_seconds', 0),
                               ('api_key_env', 'not-an-env'), ('unexpected', 1)]:
                with self.subTest(key=key, value=value):
                    path.write_text(json.dumps({'x-tracing': {**settings, key: value}}))
                    with self.assertRaises(ValueError):
                        load_config(path)

    def test_roles_tools_skills_and_actual_model_usage_share_a_trace_across_threads(self):
        from ax_local.api.phoenix import PhoenixTracer

        with tempfile.TemporaryDirectory() as tmp, receiver() as (endpoint, state):
            tracer = PhoenixTracer(self.settings(tmp, endpoint), api_key='test-secret')
            try:
                with tracer.session('a' * 16, 'planner', {'skills': ['engineering/code-review']}) as root:
                    root.event(json.dumps({'event': 'skills_loaded', 'skills': ['engineering/code-review']}))
                    root.event(json.dumps({'event': 'mcp_discovery_failed', 'server': 'github',
                                           'reason': 'deadline_exceeded', 'error': 'private-value'}))

                    def child():
                        with tracer.session('b' * 16, 'qa_engineer', {}, parent_session_id='a' * 16) as scope:
                            scope.event(json.dumps({'event': 'tool_call', 'name': 'calculate',
                                                    'call_id': 'x', 'arguments': {'password': 'private-value'}}))
                            scope.event(json.dumps({'event': 'tool_result', 'name': 'calculate', 'call_id': 'x',
                                                    'output': 'private-value'}))
                            model = tracer.model_span('b' * 16, 'requested/model')
                            model.model_result(200, json.dumps({'model': 'actual-model',
                                'usage': {'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10}}).encode())
                            scope.result({'outcome': {'status': 'completed'}})

                    thread = threading.Thread(target=child)
                    thread.start()
                    thread.join()
                    root.event(json.dumps({'event': 'tool_call', 'name': 'calculate',
                                           'call_id': 'polled-child', 'agent_session_id': 'b' * 16}))
                    root.result({'outcome': {'status': 'completed'}})
                wait_for(lambda: len(state['spans']) == 4)
                spans = state['spans']
                self.assertEqual(len({s['context']['trace_id'] for s in spans}), 1)
                parent = next(s for s in spans if s['name'] == 'planner')
                child_span = next(s for s in spans if s['name'] == 'qa_engineer')
                self.assertEqual(child_span['parent_id'], parent['context']['span_id'])
                self.assertEqual({s['attributes']['session.id'] for s in spans}, {'a' * 16})
                self.assertEqual(parent['attributes']['popot.agent_session_id'], 'a' * 16)
                self.assertEqual({s['attributes']['popot.agent_session_id'] for s in spans
                                  if s['name'] != 'planner'}, {'b' * 16})
                model = next(s for s in spans if s['span_kind'] == 'LLM')
                self.assertEqual(model['attributes']['llm.token_count.total'], 10)
                self.assertEqual(model['attributes']['llm.model_name'], 'actual-model')
                self.assertEqual(parent['attributes']['popot.skills.loaded'], ['engineering/code-review'])
                self.assertTrue(any(e['name'] == 'mcp_discovery_failed' for e in parent['events']))
                self.assertIn('private-value', json.dumps(spans))
                self.assertNotIn('test-secret', ''.join(p.read_text() for p in Path(tmp).glob('*.json')))
            finally:
                tracer.close()

    def test_late_child_after_api_restart_keeps_the_root_phoenix_session(self):
        from ax_local.api.phoenix import PhoenixTracer
        from ax_local.api.delegation import DelegationService
        from popot_agents.progress import emit_progress

        with tempfile.TemporaryDirectory() as tmp, receiver() as (endpoint, state):
            settings = self.settings(tmp, endpoint)
            first = PhoenixTracer(settings, api_key='test')
            try:
                with first.session('a' * 16, 'planner', {}) as root:
                    root.result({'outcome': {'status': 'partial'}})
                wait_for(lambda: len(state['spans']) == 1)
            finally:
                first.close()

            restarted = PhoenixTracer(settings, api_key='test')
            try:
                config = {'_ax_role': 'qa_engineer', '_ax_delegation': {
                    'parent_session_id': 'a' * 16, 'root_session_id': 'a' * 16}}
                job = SimpleNamespace(session_id='b' * 16, config=config)

                def execute_child(job):
                    emit_progress('tool_call', name='calculate', call_id='child')
                    emit_progress('tool_result', name='calculate', call_id='child')
                    model = restarted.model_span(job.session_id, 'requested/model')
                    model.model_result(200, b'{"choices": [{}]}')
                    job.config['_ax_delegation']['outcome'] = {'status': 'completed'}

                service = SimpleNamespace(tracer=restarted, _execute_observed=execute_child,
                                          _record_progress=Mock())
                DelegationService._execute(service, job)
                wait_for(lambda: len(state['spans']) == 4)
                self.assertEqual({s['attributes']['session.id'] for s in state['spans']}, {'a' * 16})
                self.assertEqual(len({s['context']['trace_id'] for s in state['spans']}), 2)
                child = next(s for s in state['spans'] if s['name'] == 'qa_engineer')
                self.assertIsNone(child['parent_id'])
                self.assertEqual(child['attributes']['popot.parent_session_id'], 'a' * 16)
                self.assertEqual(json.loads(child['attributes']['metadata'])['session_id'], 'b' * 16)
                for span in state['spans']:
                    self.assertEqual(span['attributes']['popot.root_session_id'], 'a' * 16)
                    if span['name'] != 'planner':
                        self.assertEqual(span['attributes']['popot.agent_session_id'], 'b' * 16)
            finally:
                restarted.close()

    def test_background_children_and_cron_export_without_parent_polling(self):
        from ax_local.api.phoenix import PhoenixTracer
        from ax_local.api.delegation import DelegationService
        from ax_local.api.cron import CronScheduler
        from popot_agents.progress import emit_progress

        with tempfile.TemporaryDirectory() as tmp, receiver() as (endpoint, state):
            tracer = PhoenixTracer(self.settings(tmp, endpoint), api_key='test')
            try:
                with tracer.session('a' * 16, 'planner', {}) as root:
                    config = {'_ax_role': 'qa_engineer', '_ax_delegation': {'parent_session_id': 'a' * 16}}
                    job = SimpleNamespace(session_id='b' * 16, config=config)
                    def execute_child(job):
                        emit_progress('tool_call', name='calculate', call_id='child')
                        emit_progress('tool_result', name='calculate', call_id='child')
                        job.config['_ax_delegation']['outcome'] = {'status': 'completed'}
                    service = SimpleNamespace(tracer=tracer, _execute_observed=execute_child,
                                              _record_progress=Mock())
                    thread = threading.Thread(target=DelegationService._execute, args=(service, job))
                    thread.start()
                    thread.join()
                    root.result({'outcome': {'status': 'completed'}})
                config = {'_ax_cron': {'state': 'TASK_STATE_SUBMITTED'}}
                def execute_cron(_key, _sid, config, _task):
                    emit_progress('tool_call', name='utc_time', call_id='cron')
                    emit_progress('tool_result', name='utc_time', call_id='cron')
                    config['_ax_cron']['state'] = 'TASK_STATE_COMPLETED'
                scheduler = SimpleNamespace(tracer=tracer, _execute_observed=execute_cron)
                CronScheduler._execute(scheduler, ('cron_demo', 0), 'c' * 16, config, 'time')
                wait_for(lambda: len(state['spans']) == 5)
                by_name = {s['name']: s for s in state['spans']}
                self.assertEqual(by_name['qa_engineer']['parent_id'], by_name['planner']['context']['span_id'])
                self.assertNotEqual(by_name['cron_demo']['context']['trace_id'], by_name['planner']['context']['trace_id'])
            finally:
                tracer.close()

    def test_outage_does_not_fail_execution_and_pending_spans_survive_restart(self):
        from ax_local.api.phoenix import PhoenixTracer

        with tempfile.TemporaryDirectory() as tmp, receiver() as (endpoint, state):
            state['fail'] = True
            settings = self.settings(tmp, endpoint)
            tracer = PhoenixTracer(settings, api_key='test')
            with tracer.session('a' * 16, 'qa_engineer', {}) as scope:
                scope.result({'outcome': {'status': 'completed'}})
            wait_for(lambda: state['requests'] > 0)
            tracer.close()
            self.assertEqual(len(list(Path(tmp).glob('*.json'))), 1)
            state['fail'] = False
            restarted = PhoenixTracer(settings, api_key='test')
            try:
                wait_for(lambda: len(state['spans']) == 1 and not list(Path(tmp).glob('*.json')))
                self.assertEqual(state['spans'][0]['status_code'], 'OK')
            finally:
                restarted.close()

    def test_lost_acknowledgement_does_not_duplicate_or_stall_the_outbox(self):
        from ax_local.api.phoenix import PhoenixTracer

        with tempfile.TemporaryDirectory() as tmp, receiver() as (endpoint, state):
            state['lose_ack'] = True
            tracer = PhoenixTracer(self.settings(tmp, endpoint), api_key='test')
            try:
                with tracer.session('a' * 16, 'qa_engineer', {}) as scope:
                    scope.result({'outcome': {'status': 'completed'}})
                wait_for(lambda: state['requests'] >= 2 and not list(Path(tmp).glob('*.json')))
                self.assertEqual(len(state['spans']), 1)
            finally:
                tracer.close()

    def test_root_execution_is_traced_without_a_streaming_client(self):
        from ax_local.api.phoenix import PhoenixTracer
        from popot_agents.orchestrator.workflow_execution import run_root
        from popot_agents.progress import emit_progress, progress_observer

        with tempfile.TemporaryDirectory() as tmp, receiver() as (endpoint, state):
            tracer = PhoenixTracer(self.settings(tmp, endpoint), api_key='test')
            try:
                def send(_message):
                    emit_progress('tool_call', name='calculate', call_id='x')
                    emit_progress('tool_result', name='calculate', call_id='x')
                    return {'answer': '42', 'outcome': {'status': 'completed'}}
                worker = Mock()
                worker.send.side_effect = send
                record = SimpleNamespace(agent='test', role='qa_engineer', role_config={},
                                         worker=worker, messages=[])
                server = SimpleNamespace(tracer=tracer, store=Mock())
                result = run_root(server, 'a' * 16, record, 'calculate')
                self.assertEqual(result['answer'], '42')
                self.assertIsNone(progress_observer())
                wait_for(lambda: len(state['spans']) == 2)
                self.assertEqual({s['span_kind'] for s in state['spans']}, {'AGENT', 'TOOL'})
            finally:
                tracer.close()

    def test_proxy_exports_usage_once_and_keeps_parent_when_it_finishes_first(self):
        from ax_local.api.phoenix import PhoenixTracer
        from ax_local.api.provider_proxy import Provider, start_proxy
        from urllib import request

        with tempfile.TemporaryDirectory() as tmp, receiver() as (endpoint, state):
            tracer = PhoenixTracer(self.settings(tmp, endpoint), api_key='test')
            proxy = start_proxy({'key': Provider('test', 'requested', 'http://unused', None)},
                                host='127.0.0.1', port=0, tracer=tracer)
            entered, release = threading.Event(), threading.Event()
            received = []
            body = json.dumps({'choices': [{}], 'model': 'actual',
                               'usage': {'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10}}).encode()
            def dispatch(*_args, **_kwargs):
                entered.set()
                release.wait(3)
                return 200, body
            try:
                with patch('ax_local.api.provider_proxy.dispatch_request', side_effect=dispatch):
                    with tracer.session('a' * 16, 'qa_engineer', {}) as scope:
                        def call():
                            req = request.Request(f'http://127.0.0.1:{proxy.server_port}/v1/chat/completions',
                                data=b'{}', headers={'Authorization': 'Bearer key', 'X-Popot-Session-Id': 'a' * 16})
                            with request.urlopen(req, timeout=4) as response:
                                received.append(response.read())
                        client = threading.Thread(target=call)
                        client.start()
                        self.assertTrue(entered.wait(2))
                        scope.result({'outcome': {'status': 'completed'}})
                    release.set()
                    client.join(4)
                    self.assertFalse(client.is_alive())
                    self.assertEqual(received, [body])
                wait_for(lambda: len(state['spans']) == 2)
                root = next(s for s in state['spans'] if s['span_kind'] == 'AGENT')
                model = next(s for s in state['spans'] if s['span_kind'] == 'LLM')
                self.assertEqual(model['parent_id'], root['context']['span_id'])
                self.assertEqual(model['attributes']['llm.token_count.total'], 10)
            finally:
                release.set()
                proxy.shutdown()
                proxy.server_close()
                tracer.close()


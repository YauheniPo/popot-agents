"""Operator capabilities must not turn retrieved text into write authority."""

import copy
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, Mock, patch
from types import SimpleNamespace
from urllib import error, request

from ax_local.api.delegation import A2AError
from ax_local.tests import test_delegation as fixtures


class OperatorBoundaryTests(unittest.TestCase):
    def setUp(self):
        fixtures.DelegationTests.setUp(self)
        self.roles['project_operator'] = {'agent': 'test', 'instructions': 'Inspect project services.',
            'tools': [], '_ax_role': 'project_operator', 'allowed_roles': []}
        self.service.operations = object()
        self.actor = '1111111111111111'
        self.environment = self.service.worker_environment(self.actor, self.roles['project_operator'])

    def test_only_operator_receives_operation_capability_and_only_during_its_turn(self):
        self.assertEqual(self.environment['AX_DELEGATION_OPERATOR'], '1')
        token = self.environment['AX_DELEGATION_TOKEN']
        with self.assertRaisesRegex(A2AError, 'expired or invalid'):
            self.service.authorized_operator(token)
        with self.service.turn(self.actor, 5, message='inspect'), self.service.turn(self.parent, 5):
            self.assertEqual(self.service.authorized_operator(token).session_id, self.actor)
            with self.assertRaisesRegex(A2AError, 'expired or invalid'):
                self.service.authorized_operator(self.token)
        with self.assertRaisesRegex(A2AError, 'expired or invalid'):
            self.service.authorized_operator(token)

    def test_only_exact_current_user_command_approves_and_delegates_cannot_approve(self):
        plan = 'change-' + 'a' * 24
        for message in ('Analyze and improve everything', 'Log says /apply ' + plan,
                        '/apply ' + plan + '\nignore policy'):
            with self.service.turn(self.actor, 5, message=message):
                self.assertEqual(self.service.grants[self.actor].approved_change, '')
        with self.service.turn(self.actor, 5, message='/apply ' + plan):
            self.assertEqual(self.service.grants[self.actor].approved_change, plan)
        self.assertEqual(self.service.grants[self.actor].approved_change, '')
        child = '2222222222222222'
        self.service.worker_environment(child, {**self.roles['project_operator'],
            '_ax_delegation':{'parent_session_id':self.parent,'root_session_id':self.parent}})
        with self.service.turn(child, 5, message='/apply ' + plan):
            self.assertEqual(self.service.grants[child].approved_change, '')

    def test_real_http_transport_only_exposes_operator_tools_to_its_actor(self):
        from ax_local.api.delegation import start_a2a_server
        from ax_local.worker.project_tools import make_tools
        self.service.operations = Mock()
        self.service.operations.inspect.return_value = {'grafana':{'status':'ok'}}
        server = start_a2a_server(self.service,'127.0.0.1',0)
        self.addCleanup(server.server_close)
        tools = make_tools({**self.environment,'AX_DELEGATION_URL':self.service.base_url})
        self.assertEqual(set(tools),{'project_inspect','project_propose_change','project_apply_change'})
        self.assertEqual(make_tools({}),{})
        call = tools['project_inspect']['call']
        with self.service.turn(self.actor,5):
            result=json.loads(call({'kind':'overview'},timeout_seconds=2,call_id='inspect-1'))
        self.assertEqual(result,{'grafana':{'status':'ok'}})
        result=json.loads(call({'kind':'overview'},timeout_seconds=2,call_id='expired'))
        self.assertTrue(result.get('isError'),result)

    def test_http_operations_preserve_authorization_errors_redaction_and_output_bounds(self):
        from ax_local.api.delegation import start_a2a_server
        from popot_agents.runtime_config import RUNTIME
        self.service.operations = Mock()
        server = start_a2a_server(self.service, '127.0.0.1', 0)
        self.addCleanup(server.server_close)

        def call(body):
            invocation = request.Request(self.service.base_url + '/operations',
                data=json.dumps(body).encode(), headers={
                    'Content-Type': 'application/json',
                    'Authorization': 'Bearer ' + self.environment['AX_DELEGATION_TOKEN']})
            try:
                with request.urlopen(invocation, timeout=3) as response:
                    return response.status, json.load(response)
            except error.HTTPError as response:
                with response:
                    return response.code, json.load(response)

        with self.service.turn(self.actor, 10):
            for body in ([], {'params': []}, {'method': 'unknown'}):
                with self.subTest(body=body):
                    self.assertEqual(call(body)[0], 400)
            self.service.operations.propose.return_value = {'plan_id': 'test-plan', 'token': 'hidden'}
            status, body = call({'method': 'propose_change', 'params': {'action': 'restart_service'}})
            self.assertEqual((status, body), (200, {'plan_id': 'test-plan', 'token': '[REDACTED]'}))
            self.service.operations.propose.assert_called_once_with(self.actor, {'action': 'restart_service'})
            self.service.operations.apply.return_value = {'status': 'applied'}
            self.assertEqual(call({'method': 'apply_change', 'params': {'plan_id': 'test-plan'}})[0], 200)
            options = self.service.operations.apply.call_args.kwargs
            self.assertEqual(options['approved'], '')
            self.assertEqual(options['authorize']().session_id, self.actor)
            self.service.operations.inspect.return_value = {'details': 'x' * 2000}
            with patch.dict(RUNTIME['limits'], {'tool_output_chars': 400}):
                status, body = call({'method': 'inspect', 'params': {'kind': 'overview'}})
            self.assertEqual(status, 200)
            self.assertTrue(body['truncated'])
            self.assertLess(len(body['preview']), 400)
            for failure, expected in ((ValueError('invalid query'), 400),
                                      (RuntimeError('private provider failure'), 502),
                                      (A2AError('expired'), 403)):
                self.service.operations.inspect.side_effect = failure
                status, body = call({'method': 'inspect', 'params': {'kind': 'overview'}})
                self.assertEqual(status, expected)
                self.assertNotIn('private provider failure', json.dumps(body))


class OperatorChangeTests(unittest.TestCase):
    def setUp(self):
        from ax_local.api.project_operator import ProjectOperations
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'config/omniroute').mkdir(parents=True)
        source = Path(__file__).resolve().parents[2] / 'config/omniroute/combos.json'
        self.combos = json.loads(source.read_text())
        (self.root / 'config/omniroute/combos.json').write_text(json.dumps(self.combos))
        self.monitoring={'grafana':{'environment':{'GF_USERS_ALLOW_SIGN_UP':'false'}},
                         'prometheus':{'retention_time':'7d'}}
        (self.root / 'config/monitoring.json').write_text(json.dumps(self.monitoring))
        self.ops = ProjectOperations({}, None, root=self.root)
        bundle_patch = patch('ax_local.cluster.holmes.omniroute_ca_bundle', return_value='public-ca-bundle')
        bundle_patch.start()
        self.addCleanup(bundle_patch.stop)
        gateway = Mock()
        gateway.request.return_value = {'combos':list(self.combos.values())}
        gateway_patch = patch.object(self.ops,'_gateway',return_value=gateway)
        gateway_patch.start()
        self.addCleanup(gateway_patch.stop)

    def test_combo_plan_needs_exact_approval_is_deduplicated_and_persists(self):
        combo = self.combos['ax-free']
        desired = copy.deepcopy(combo)
        desired['models'] = list(reversed(desired['models']))
        with patch.object(self.ops, 'read_combo', return_value=combo), \
             patch.object(self.ops, 'write_combo') as write:
            plan = self.ops.propose('actor', {'action': 'configure_combo', 'name': 'ax-free',
                'models': [m['model'] for m in desired['models']]})
            self.assertEqual(plan['after']['models'], desired['models'])
            with self.assertRaisesRegex(ValueError, 'approval'):
                self.ops.apply('actor', plan['plan_id'], approved='')
            with self.assertRaisesRegex(ValueError, 'another session'):
                self.ops.apply('other', plan['plan_id'], approved=plan['plan_id'])
            self.ops.apply('actor', plan['plan_id'], approved=plan['plan_id'])
            self.ops.apply('actor', plan['plan_id'], approved=plan['plan_id'])
            write.assert_called_once()
        saved = json.loads((self.root / 'config/omniroute/combos.json').read_text())
        self.assertEqual(saved['ax-free']['models'], desired['models'])

    def test_paid_or_unknown_models_and_stale_plans_are_rejected(self):
        combo = self.combos['ax-free']
        with patch.object(self.ops, 'read_combo', return_value=combo), \
             patch.object(self.ops, 'write_combo') as write:
            with self.assertRaisesRegex(ValueError, 'approved model'):
                self.ops.propose('actor', {'action':'configure_combo','name':'ax-free',
                                         'models':['openrouter/paid-model']})
            plan = self.ops.propose('actor', {'action':'configure_combo','name':'ax-free',
                                            'models':[combo['models'][-1]['model']]})
            with patch.object(self.ops, 'read_combo', return_value={**combo,'strategy':'round-robin'}):
                with self.assertRaisesRegex(ValueError, 'changed'):
                    self.ops.apply('actor',plan['plan_id'],approved=plan['plan_id'])
            write.assert_not_called()

    def test_resource_changes_require_approval_and_persist_only_supported_services(self):
        before={'requests':{'cpu':'100m','memory':'128Mi'},'limits':{'cpu':'1','memory':'512Mi'}}
        after={**before,'limits':{'cpu':'1500m','memory':'768Mi'}}
        deployment={'metadata':{'resourceVersion':'42'},'spec':{'template':{'metadata':{},
            'spec':{'containers':[{'name':'grafana','resources':before}]}}}}
        changed=copy.deepcopy(deployment);changed['spec']['template']['spec']['containers'][0]['resources']=after
        with patch.object(self.ops,'_deployment',side_effect=[deployment,deployment,changed]), \
             patch.object(self.ops,'_kube',return_value='{}') as kube:
            plan=self.ops.propose('actor',{'action':'set_resources','service':'grafana','resources':after})
            result=self.ops.apply('actor',plan['plan_id'],approved=plan['plan_id'])
            self.assertEqual(result['status'],'applied')
            self.assertIn('resourceVersion',kube.call_args.args[-1])
        expected=copy.deepcopy(self.monitoring)
        expected['grafana']['resources']=after
        self.assertEqual(json.loads((self.root/'config/monitoring.json').read_text()),expected)

    def test_resource_plan_rejects_changed_monitoring_settings_without_overwriting_them(self):
        before={'requests':{'cpu':'100m','memory':'128Mi'},'limits':{'cpu':'1','memory':'512Mi'}}
        deployment={'metadata':{'resourceVersion':'42'},'spec':{'template':{'metadata':{},
            'spec':{'containers':[{'name':'grafana','resources':before}]}}}}
        with patch.object(self.ops,'_deployment',return_value=deployment), \
             patch.object(self.ops,'_kube') as kube:
            plan=self.ops.propose('actor',{'action':'set_resources','service':'grafana','resources':before})
            changed=copy.deepcopy(self.monitoring)
            changed['prometheus']['retention_time']='14d'
            path=self.root/'config/monitoring.json'
            path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError,'changed'):
                self.ops.apply('actor',plan['plan_id'],approved=plan['plan_id'])
            kube.assert_not_called()
            self.assertEqual(json.loads(path.read_text()),changed)

    def test_expired_plan_and_failed_or_unknown_write_are_not_retried(self):
        combo=self.combos['ax-free']
        with patch.object(self.ops,'read_combo',return_value=combo), \
             patch.object(self.ops,'write_combo',side_effect=TimeoutError) as write:
            plan=self.ops.propose('actor',{'action':'configure_combo','name':'ax-free',
                                         'models':[combo['models'][0]['model']]})
            first=self.ops.apply('actor',plan['plan_id'],approved=plan['plan_id'])
            self.assertEqual(first['status'],'failed_or_unknown')
            self.assertTrue(first['isError'])
            self.assertEqual(first,self.ops.apply('actor',plan['plan_id'],approved=plan['plan_id']))
            write.assert_called_once()
            self.ops.plans[plan['plan_id']]['expires']=0
            with self.assertRaisesRegex(ValueError,'expired'):
                self.ops.apply('actor',plan['plan_id'],approved=plan['plan_id'])

    def test_concurrent_apply_of_one_plan_mutates_only_once(self):
        combo=self.combos['ax-free']
        barrier=threading.Barrier(2)
        results=[]
        with patch.object(self.ops,'read_combo',return_value=combo), \
             patch.object(self.ops,'write_combo') as write:
            plan=self.ops.propose('actor',{'action':'configure_combo','name':'ax-free',
                                         'models':[combo['models'][0]['model']]})
            def apply():
                barrier.wait(timeout=2)
                results.append(self.ops.apply('actor',plan['plan_id'],approved=plan['plan_id']))
            threads=[threading.Thread(target=apply) for _ in range(2)]
            for thread in threads:thread.start()
            for thread in threads:thread.join(timeout=3)
            self.assertEqual(len(results),2)
            self.assertEqual(results[0],results[1])
            write.assert_called_once()

    def test_http_secrets_are_removed_and_arbitrary_paths_and_shell_are_not_supported(self):
        with self.assertRaises(ValueError):
            self.ops.inspect('shell', {'command':'kubectl delete pods --all'})
        with self.assertRaises(ValueError):
            self.ops.inspect('logs', {'namespace':'other-cluster','pod':'bad; env'})
        from ax_local.api.project_operator import safe_result
        result = safe_result({'apiKey':'private-key','provider':'openrouter',
            'nested':{'accessToken':'secret','password':'hidden'},'message':'api_key=abc123'})
        self.assertNotIn('private-key', json.dumps(result))
        self.assertNotIn('secret', json.dumps(result))

    def test_holmes_uses_real_pinned_job_manifests_and_returns_its_report(self):
        from ax_local.cluster.holmes import IMAGE
        calls=[]
        def kube(*args,payload=None):
            calls.append((args,payload))
            if 'jobs' in args:return json.dumps({'items':[]})
            if args[0]=='create':return json.dumps({'metadata':{'name':'holmes-ax-test'}})
            if 'job' in args:return json.dumps({'status':{'succeeded':1}})
            if 'logs' in args:return 'Holmes observed current pod readiness and logs.'
            return '{}'
        with patch.dict('os.environ',{'HOLMES_MODEL':'ax-free','OMNIROUTE_API_KEY':'fake-gateway'}), \
             patch.object(self.ops,'_kube',side_effect=kube):
            report=self.ops.inspect('holmes',{'question':'Investigate AX pods'},actor='actor',request_id='request')
        job=next(p for a,p in calls if a[0]=='create')
        self.assertEqual(job['spec']['template']['spec']['containers'][0]['image'],IMAGE)
        self.assertEqual(job['metadata']['labels']['popot.dev/operator-session'],'actor')
        self.assertIn('Holmes observed',report['report'])
        self.assertEqual(report['status'],{'succeeded':1})
        self.assertNotIn('fake-gateway',json.dumps(report))

    def test_resource_validation_rejects_excessive_limits_and_request_limit_inversion(self):
        from ax_local.resource_config import validate_resources
        for resources in (
            {},
            {'requests':{'cpu':'100m','memory':'128Mi'},'limits':{'memory':'512Mi'}},
            {'requests':{'cpu':'1','memory':'128Mi'},'limits':{'cpu':'5','memory':'512Mi'}},
            {'requests':{'cpu':'2','memory':'128Mi'},'limits':{'cpu':'1','memory':'512Mi'}},
            {'requests':{'cpu':'100m','memory':'512Mi'},'limits':{'cpu':'1','memory':'128Mi'}},
        ):
            with self.subTest(resources=resources),self.assertRaises(ValueError):
                validate_resources(resources)

    def test_concurrent_holmes_start_for_same_call_creates_only_one_job(self):
        jobs=[]
        reports=[]
        barrier=threading.Barrier(2)
        def kube(*args,payload=None):
            if 'jobs' in args:
                snapshot=copy.deepcopy(jobs)
                time.sleep(0.05)
                return json.dumps({'items':snapshot})
            if args[0]=='create':
                job={'metadata':{'name':'holmes-ax-once'}}
                jobs.append(job)
                return json.dumps(job)
            if 'job' in args:return json.dumps({'status':{'succeeded':1}})
            if 'logs' in args:return 'completed'
            return '{}'
        def start():
            barrier.wait(timeout=2)
            reports.append(self.ops.inspect('holmes',{'question':'Check pods'},actor='actor',request_id='same'))
        with patch.dict('os.environ',{'HOLMES_MODEL':'ax-ops-free','OMNIROUTE_API_KEY':'fake-gateway'}), \
             patch.object(self.ops,'_kube',side_effect=kube):
            threads=[threading.Thread(target=start) for _ in range(2)]
            for thread in threads:thread.start()
            for thread in threads:thread.join(timeout=3)
        self.assertEqual(len(reports),2)
        self.assertEqual(len(jobs),1)

    def test_gateway_default_fields_do_not_make_a_verified_combo_write_fail(self):
        combo=copy.deepcopy(self.combos['ax-free'])
        enriched=copy.deepcopy(combo)
        for model in enriched['models']:model['weight']=0
        gateway=Mock();gateway.request.return_value={'combos':[{'name':'ax-free','id':'route'}]}
        with patch.object(self.ops,'_gateway',return_value=gateway), \
             patch.object(self.ops,'read_combo',return_value=enriched):
            self.ops.write_combo('ax-free',combo)
        self.assertEqual(gateway.request.call_args.args[0],'PUT')


class OperatorInspectionTests(unittest.TestCase):
    def setUp(self):
        OperatorChangeTests.setUp(self)

    def test_only_managed_configuration_files_can_be_saved(self):
        outside = self.root / 'outside.json'
        outside.write_text('preserve')
        for name in ('../outside.json', str(outside), 'roles.json'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'managed configuration'):
                self.ops._save(name, {'unexpected': True})
            self.assertEqual(outside.read_text(), 'preserve')
        self.ops._save('monitoring.json', self.monitoring)
        self.assertEqual(json.loads((self.root / 'config/monitoring.json').read_text()), self.monitoring)
        combos = copy.deepcopy(self.combos)
        combos['ax-free']['description'] = '../outside.json'
        self.ops._save('omniroute/combos.json', combos)
        self.assertEqual(json.loads((self.root / 'config/omniroute/combos.json').read_text()), combos)
        self.assertEqual(outside.read_text(), 'preserve')

    def test_kubernetes_reads_are_bounded_and_do_not_return_subprocess_errors(self):
        with patch('ax_local.api.project_operator.subprocess.run') as run:
            run.return_value = SimpleNamespace(returncode=0, stdout='{"items": []}')
            self.assertEqual(json.loads(self.ops._kube('get', 'pods')), {'items': []})
            self.assertIn('--request-timeout=10s', run.call_args.args[0])
            self.assertEqual(run.call_args.kwargs['timeout'], 12)
            self.ops._kube('apply', '-f', '-', payload={'kind': 'List'})
            self.assertEqual(json.loads(run.call_args.kwargs['input']), {'kind': 'List'})
            run.return_value = SimpleNamespace(returncode=1, stdout='private value')
            with self.assertRaisesRegex(RuntimeError, 'inspect service health') as result:
                self.ops._kube('get', 'pods')
            self.assertNotIn('private value', str(result.exception))
            run.return_value = SimpleNamespace(returncode=0, stdout='x' * (512 * 1024 + 1))
            with self.assertRaisesRegex(ValueError, 'too large'):
                self.ops._kube('get', 'pods')
            from ax_local.config import AX_CONFIG
            with patch.dict(AX_CONFIG, {'context': 'production'}):
                with self.assertRaisesRegex(ValueError, 'local kind'):
                    self.ops._kube('get', 'pods')

    def test_service_http_reads_bound_the_body_and_keep_redirects_disabled(self):
        with patch('ax_local.api.project_operator.request.build_opener') as build:
            opener = build.return_value
            opener.open.return_value = io.BytesIO(b'{"healthy":true}')
            self.assertEqual(self.ops._json('http://service/health'), {'healthy': True})
            self.assertEqual(opener.open.call_args.kwargs['timeout'], 8)
            self.assertEqual(type(build.call_args.args[0]).__name__, 'NoRedirect')
            opener.open.return_value = io.BytesIO(b'x' * (512 * 1024 + 1))
            with self.assertRaisesRegex(ValueError, 'too large'):
                self.ops._json('http://service/health')
            opener.open.return_value = io.BytesIO(b'not-json')
            with self.assertRaises(json.JSONDecodeError):
                self.ops._json('http://service/health')
            response = Mock(status=200, headers={'Content-Type': 'text/html'})
            opener.open.return_value = MagicMock()
            opener.open.return_value.__enter__.return_value = response
            self.assertEqual(self.ops._http_status('http://service/ui'),
                             {'http_status': 200, 'content_type': 'text/html'})
        with patch('ax_local.api.project_operator.socket.create_connection') as connect:
            self.assertTrue(self.ops._mcp_status()['tcp_reachable'])
            connect.assert_called_once_with(('mcp-server', 8001), timeout=3)
        with patch.object(self.ops, '_json', return_value={'status': 'ok'}) as read, \
             patch.dict('os.environ', {'AX_LOCAL_BIND_HOST': 'api', 'AX_LOCAL_API_PORT': '8002'}):
            self.assertEqual(self.ops._api_health(), {'status': 'ok'})
            read.assert_called_once_with('http://api:8002/healthz')
        with patch('ax_local.api.project_operator.Api') as api, \
             patch.dict('os.environ', {'OMNIROUTE_INITIAL_PASSWORD': 'test-password'}):
            type(self.ops)._gateway(self.ops)
            api.return_value.request.assert_called_once_with(
                'POST', '/api/auth/login', {'password': 'test-password'})
        with patch('popot_agents.worker.cognee_memory.CogneeMemory') as memory, \
             patch.dict('os.environ', {'COGNEE_API_URL': 'http://memory', 'COGNEE_PASSWORD': 'test-password'}):
            self.ops._memory()
            memory.assert_called_once_with('http://memory', 'test-password', timeout=8)

    def test_kubernetes_inspections_validate_scope_and_preserve_diagnostic_evidence(self):
        namespace = self.ops.namespaces[0]
        pod = {'metadata': {'name': 'ax-worker'}, 'status': {'phase': 'Running'},
               'spec': {'containers': [{'name': 'agent', 'resources': {'limits': {'cpu': '1'}}}]}}
        event = {'type': 'Warning', 'reason': 'BackOff', 'message': 'api_key=hidden',
                 'involvedObject': {'name': 'ax-worker'}}
        with patch.object(self.ops, '_kube', return_value=json.dumps({'items': [pod]})) as kube:
            rows = self.ops.inspect('pods', {'namespace': namespace})
            self.assertEqual(rows[0]['pod'], 'ax-worker')
            self.assertEqual(rows[0]['resources'][0]['resources'], {'limits': {'cpu': '1'}})
            kube.return_value = json.dumps({'items': [event]})
            self.assertEqual(self.ops.inspect('events', {'namespace': namespace})[0]['reason'], 'BackOff')
            kube.return_value = 'worker restarted; api_key=hidden'
            result = self.ops.inspect('logs', {'namespace': namespace, 'pod': 'ax-worker',
                                             'container': 'agent', 'previous': True})
            self.assertIn('--previous', kube.call_args.args)
            self.assertIn('worker restarted', result['logs'])
            self.assertNotIn('hidden', result['logs'])
        for params in ({'namespace': 'unrelated'}, {'pod': '../escape', 'container': 'agent'},
                       {'pod': 'ax-worker', 'container': '--all-containers'}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.ops.inspect('logs', params)
        with self.assertRaisesRegex(ValueError, 'object'):
            self.ops.inspect('pods', [])

    def test_metrics_models_memory_and_sessions_use_only_their_bounded_read_routes(self):
        with patch.object(self.ops, '_proxy', return_value={'result': 'ok'}) as proxy:
            self.ops.inspect('metrics', {'query': 'up'})
            self.assertIn('/api/v1/query?', proxy.call_args.args[1])
            self.ops.inspect('metrics', {'query': 'up', 'start': 0, 'end': 300, 'step': 30})
            self.assertIn('/api/v1/query_range?', proxy.call_args.args[1])
            for params in ({}, {'query': []}, {'query': 'x' * 3001},
                           {'query': 'up', 'start': 0}, {'query': 'up', 'start': 0, 'end': 900000},
                           {'query': 'up', 'start': 0, 'end': 300, 'step': 1}):
                with self.subTest(params=params), self.assertRaises(ValueError):
                    self.ops.inspect('metrics', params)
        with patch.object(self.ops, '_gateway') as gateway:
            gateway.return_value.request.return_value = {'token': 'hidden'}
            models = self.ops.inspect('models', {})
            self.assertEqual(set(models), {'combos', 'health', 'usage', 'combo_metrics'})
            self.assertNotIn('hidden', json.dumps(models))
        with patch.object(self.ops, '_memory') as memory:
            memory.return_value._request.return_value = {'count': 2}
            self.assertEqual(self.ops.inspect('memory', {'range': '7d'})['stats'], {'count': 2})
            self.assertIn('range=7d', memory.return_value._request.call_args_list[0].args[0])
            with self.assertRaisesRegex(ValueError, 'time range'):
                self.ops.inspect('memory', {'range': '../other'})
        self.ops.store = MagicMock()
        self.ops.store.list_chats.return_value = [{'sessionId': 's', 'role': 'test', 'token': 'hidden'}]
        self.ops.store._connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = (100, 2)
        sessions = self.ops.inspect('sessions', {})
        self.assertEqual(sessions, {'total': 1, 'recent': [{'sessionId': 's', 'role': 'test'}],
                                   'postgres': {'database_bytes': 100, 'connections': 2}})
        self.ops.store = None
        self.assertEqual(self.ops.inspect('sessions', {})['postgres'], {'backend': 'file'})

    def test_overview_is_partial_when_one_service_fails_and_redacts_service_details(self):
        self.ops.roles = {'test': {'agent': 'omniroute', 'env_names': ['SECRET']}}
        with patch.object(self.ops, '_api_health', return_value={'status': 'ok'}), \
             patch.object(self.ops, '_mcp_status', side_effect=TimeoutError), \
             patch.object(self.ops, '_json', return_value={'apiKey': 'hidden'}), \
             patch.object(self.ops, '_http_status', return_value={'http_status': 200}), \
             patch.object(self.ops, '_proxy', return_value={'ready': True}):
            overview = self.ops.inspect('overview', {})
        self.assertEqual(overview['ax_api'], {'status': 'ok'})
        self.assertEqual(overview['mcp_gateway'], {'unavailable': True, 'error_type': 'TimeoutError'})
        self.assertEqual(overview['roles'], {'test': {'agent': 'omniroute'}})
        self.assertNotIn('hidden', json.dumps(overview))

    def test_restart_preserves_annotations_and_uses_an_optimistic_resource_version_check(self):
        deployment = {'metadata': {'resourceVersion': '42'}, 'spec': {'template': {
            'metadata': {'annotations': {'keep': 'value'}}, 'spec': {'containers': [{'resources': {}}]}}}}
        current = copy.deepcopy(deployment)

        def patch_deployment(*args):
            operations = json.loads(args[-1])
            self.assertEqual(operations[0], {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '42'})
            current['spec']['template']['metadata']['annotations'] = operations[1]['value']

        with patch.object(self.ops, '_deployment', side_effect=lambda _: copy.deepcopy(current)), \
             patch.object(self.ops, '_kube', side_effect=patch_deployment) as kube:
            plan = self.ops.propose('actor', {'action': 'restart_service', 'service': 'grafana'})
            with self.assertRaisesRegex(ValueError, 'no longer approves'):
                self.ops.apply('actor', plan['plan_id'], approved=plan['plan_id'],
                               authorize=lambda: SimpleNamespace(approved_change=''))
            kube.assert_not_called()
            result = self.ops.apply('actor', plan['plan_id'], approved=plan['plan_id'])
            self.assertEqual(result['status'], 'applied')
            self.assertEqual(current['spec']['template']['metadata']['annotations']['keep'], 'value')
            self.assertIn('popot.dev/restarted-at', current['spec']['template']['metadata']['annotations'])
            kube.assert_called_once()
        self.assertEqual(json.loads((self.root / 'config/monitoring.json').read_text()), self.monitoring)

    def test_holmes_polling_validates_the_job_and_handles_a_pod_that_is_not_ready(self):
        with patch.dict('os.environ', {'HOLMES_MODEL': 'ax-ops-free'}), \
             patch.object(self.ops, '_kube') as kube, patch('ax_local.api.project_operator.time.sleep') as sleep:
            kube.side_effect = ['{}', '{"status":{"failed":1}}', RuntimeError('not ready')]
            result = self.ops.inspect('holmes', {'job': 'holmes-ax-pending', 'wait_seconds': 1})
            self.assertEqual(result['status'], {'failed': 1})
            self.assertIn('not ready', result['report'])
            sleep.assert_called_once_with(1)
            for params in ({'job': '../other'}, {'job': 'holmes-ax-pending', 'wait_seconds': 20},
                           {'question': 'Inspect pods'}):
                kube.side_effect = None
                kube.return_value = '{}'
                with self.subTest(params=params), self.assertRaises(ValueError):
                    self.ops.inspect('holmes', params)


class OperatorRoleTests(unittest.TestCase):
    def test_operator_uses_dedicated_strong_free_route_without_changing_other_roles(self):
        root=Path(__file__).resolve().parents[2]
        roles={path.stem:json.loads(path.read_text()) for path in (root/'config/roles').glob('*.json')}
        agents=json.loads((root/'config/agents.json').read_text())
        combos=json.loads((root/'config/omniroute/combos.json').read_text())
        self.assertEqual(roles['project_operator']['agent'],'omniroute_ops')
        self.assertEqual(agents['omniroute_ops']['model_env'],'OMNIROUTE_OPS_MODEL')
        self.assertEqual(combos['ax-ops-free']['models'][0]['model'],
                         'openrouter/nvidia/nemotron-3.5-super-vl:free')
        self.assertEqual(roles['chat']['agent'],'omniroute')


if __name__ == '__main__':
    unittest.main()

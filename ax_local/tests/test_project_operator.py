"""Operator capabilities must not turn retrieved text into write authority."""

import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

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
        with self.assertRaises(Exception):
            self.service.authorized_operator(token)
        with self.service.turn(self.actor, 5, message='inspect'), self.service.turn(self.parent, 5):
            self.assertEqual(self.service.authorized_operator(token).session_id, self.actor)
            with self.assertRaises(Exception):
                self.service.authorized_operator(self.token)
        with self.assertRaises(Exception):
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


class OperatorChangeTests(unittest.TestCase):
    def setUp(self):
        from ax_local.api.project_operator import ProjectOperations
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'config').mkdir()
        source = Path(__file__).resolve().parents[2] / 'config/omniroute-combos.json'
        self.combos = json.loads(source.read_text())
        (self.root / 'config/omniroute-combos.json').write_text(json.dumps(self.combos))
        self.monitoring={'grafana':{'environment':{'GF_USERS_ALLOW_SIGN_UP':'false'}},
                         'prometheus':{'retention_time':'7d'}}
        (self.root / 'config/monitoring.json').write_text(json.dumps(self.monitoring))
        self.ops = ProjectOperations({}, None, root=self.root)
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
        saved = json.loads((self.root / 'config/omniroute-combos.json').read_text())
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


class OperatorRoleTests(unittest.TestCase):
    def test_operator_uses_dedicated_strong_free_route_without_changing_other_roles(self):
        root=Path(__file__).resolve().parents[2]
        roles=json.loads((root/'config/roles.json').read_text())
        agents=json.loads((root/'config/agents.json').read_text())
        combos=json.loads((root/'config/omniroute-combos.json').read_text())
        self.assertEqual(roles['project_operator']['agent'],'omniroute_ops')
        self.assertEqual(agents['omniroute_ops']['model_env'],'OMNIROUTE_OPS_MODEL')
        self.assertEqual(combos['ax-ops-free']['models'][0]['model'],
                         'openrouter/nvidia/nemotron-3.5-super-vl:free')
        self.assertEqual(roles['chat']['agent'],'omniroute')


if __name__ == '__main__':
    unittest.main()

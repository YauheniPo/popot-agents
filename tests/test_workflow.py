"""Autonomous turns continue from receipts; unknown execution is never replayed."""
import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from urllib import request, error

from popot_agents.orchestrator.main import create_server
from popot_agents.orchestrator.workflow import public_workflow, public_outcome_reason

POLICY = {'max_turns':4,'total_seconds':3600,
          'turn_seconds':900,'max_no_progress':2,'max_concurrent':1}

def result(status, evidence=('receipt',), remaining=(), errors=()):
    return {'answer':status, 'outcome':{'status':status,'reason':'test',
        'evidence':list(evidence),'remaining_work':list(remaining),'errors':list(errors)}}


class WorkflowTests(unittest.TestCase):
    def test_list_summary_exposes_bounded_redacted_terminal_reason(self):
        from unittest.mock import patch
        saved={'roleConfig':{'_workflow':{'state':'blocked','phase':'finished','turns':4,
            'deadlineAt':1000,'outcome':{'reason':'workflow_turn_limit'}}}}
        self.assertEqual(public_workflow(saved)['reason'],'workflow_turn_limit')
        with patch.dict('os.environ',{'REPORT_TEST_TOKEN':'test-secret-value'}):
            reason=public_outcome_reason({'reason':'Missing test-secret-value permission '+('x'*500)})
        self.assertNotIn('test-secret-value',reason)
        self.assertLessEqual(len(reason),240)

    def run_workflow(self, results, *, allow_error=False, after=None, request_id=None, role="planner", coordination=None,
                     checkpoint=None, delegation=None, started_configs=None, disable_delegation=None):
        class Chat:
            container_name='test-coordinator'
            def __init__(self):self.calls=[]
            def send(self, message, *, timeout_seconds=None):
                self.calls.append((message,timeout_seconds))
                item=results.pop(0)
                if isinstance(item,Exception):raise item
                return item() if callable(item) else item
            def get_checkpoint(self):return checkpoint or {'messages':[{'role':'tool','content':'receipt'}]}
            def is_alive(self):return True
            def close(self):pass
        chat=Chat()
        class Runner:
            def __init__(self):self.delegation=delegation
            def start_chat(self,*args,**kwargs):
                if started_configs is not None:started_configs.append(args[1] if len(args)>1 else None)
                return chat
        with tempfile.TemporaryDirectory() as directory:
            server=create_server({'test':Runner()},roles={'analyst':{'agent':'test'}, role:{'agent':'test','instructions':'', 'coordination': coordination if coordination is not None else {}}},
                session_dir=directory,host='127.0.0.1',port=0,workflow_policy=POLICY)
            self.server=server
            thread=threading.Thread(target=server.serve_forever);thread.start()
            try:
                payload={'role':role,'task':'implement'}
                if request_id:payload['requestId']=request_id
                if disable_delegation is not None:payload['disableDelegation']=disable_delegation
                call=request.Request(f'http://127.0.0.1:{server.server_port}/tasks',
                    data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
                try:
                    with request.urlopen(call,timeout=3) as response:data=json.load(response)
                except error.HTTPError as exc:
                    if not allow_error:raise
                    data=json.load(exc)
                    data['sessionId']=server.store.list_chats()[0]['sessionId']
                if after:after(data)
                saved=server.store.get(data['sessionId'])
                return chat.calls,data,saved
            finally:
                server.shutdown();thread.join();server.server_close()

    def test_partial_is_internal_and_same_coordinator_continues_to_completion(self):
        calls,data,saved=self.run_workflow([result('partial',remaining=('review',)),result('completed')])
        self.assertEqual(len(calls),2)
        self.assertEqual(data['outcome']['status'],'completed')
        self.assertIn('receipt',calls[1][0])
        self.assertIn('Do not repeat',calls[1][0])
        self.assertEqual(saved['roleConfig']['_workflow']['state'],'completed')
        self.assertEqual(saved['roleConfig']['_workflow']['turns'],2)
        self.assertLessEqual(calls[0][1],900)

    def test_new_user_run_does_not_inherit_terminal_workflow_checkpoint(self):
        observed = {}
        def second_run():
            current = self.server.store.get(observed['session_id'])
            observed['checkpoint'] = current['roleConfig']['_workflow'].get('checkpoint')
            return result('completed')
        def after(first):
            observed['session_id'] = first['sessionId']
            call = request.Request(f'http://127.0.0.1:{self.server.server_port}/messages',
                data=json.dumps({'sessionId': first['sessionId'], 'message': 'Verify the new head'}).encode(),
                headers={'Content-Type': 'application/json'})
            with request.urlopen(call, timeout=3) as response:
                self.assertEqual(json.load(response)['outcome']['status'], 'completed')
        old_checkpoint = {'messages': [{'role': 'tool', 'content': 'old error'}],
                          'failed_delegations': [[['backend_engineer', 'old task'], {
                              'session_id': 'a' * 16, 'pending': False, 'error': 'old error'}]]}
        _, _, saved = self.run_workflow([result('blocked', errors=('old error',)), second_run],
                                       checkpoint=old_checkpoint, after=after)
        self.assertIsNone(observed['checkpoint'])
        self.assertEqual(saved['roleConfig']['_workflow']['state'], 'completed')

    def test_explicit_backend_role_can_coordinate_with_its_own_turn_limit(self):
        calls, data, saved = self.run_workflow(
            [result('partial', remaining=('review',)), result('completed')],
            role='backend_engineer', coordination={'turn_seconds': 120})
        self.assertEqual(len(calls), 2)
        self.assertEqual(data['role'], 'backend_engineer')
        self.assertLessEqual(calls[0][1], 120)
        self.assertEqual(saved['roleConfig']['_workflow']['state'], 'completed')

    def test_root_call_id_reaches_worker_metrics_config(self):
        configs=[]
        self.run_workflow([result('completed')],request_id='call-root',started_configs=configs)
        self.assertEqual(configs[0]['_ax_metrics_call_id'],'call-root')

    def test_request_id_cannot_reuse_a_task_with_different_delegation_setting(self):
        configs = []
        def retry(_first):
            call = request.Request(f'http://127.0.0.1:{self.server.server_port}/tasks',
                data=json.dumps({'role': 'planner', 'task': 'implement', 'requestId': 'call-solo',
                                 'disableDelegation': False}).encode(),
                headers={'Content-Type': 'application/json'})
            with self.assertRaises(error.HTTPError) as caught:
                request.urlopen(call, timeout=3)
            self.assertEqual(caught.exception.code, 409)
        _, _, saved = self.run_workflow([result('completed')], request_id='call-solo',
            disable_delegation=True, started_configs=configs, after=retry)
        self.assertTrue(configs[0]['_disable_delegation'])
        self.assertEqual(configs[0]['allowed_roles'], [])
        self.assertTrue(saved['roleConfig']['_disable_delegation'])

    def test_role_without_coordination_settings_uses_shared_defaults(self):
        calls, data, saved = self.run_workflow([result('partial', remaining=('review',)), result('completed')],
            role='backend_engineer', coordination={})
        self.assertEqual(len(calls), 2)
        self.assertEqual(data['outcome']['status'], 'completed')
        self.assertEqual(saved['roleConfig']['_workflow']['turns'], 2)

    def test_legacy_saved_coordination_switch_does_not_disable_or_break_session(self):
        from popot_agents.workflow_config import role_policy
        for enabled in (True, False):
            self.assertEqual(role_policy({'coordination': {'enabled': enabled, 'turn_seconds': 120}},
                                         POLICY, stored=True)['turn_seconds'], 120)

    def test_coordination_settings_reject_invalid_fields_and_limits(self):
        from popot_agents.workflow_config import role_policy
        for settings in ({'enabled': True}, {'max_turns': 0},
                         {'max_concurrent': 10}, {'turn_seconds': 99999}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                role_policy({'coordination': settings}, POLICY)

    def test_user_blocker_stops_without_automatic_retry(self):
        calls,data,_=self.run_workflow([result('blocked',remaining=('user decision',))])
        self.assertEqual(len(calls),1)
        self.assertEqual(data['outcome']['status'],'blocked')

    def test_repeated_partial_stops_at_no_progress_limit(self):
        calls,data,_=self.run_workflow([result('partial',remaining=('review',)),result('partial',remaining=('review',))])
        self.assertEqual(len(calls),2)
        self.assertEqual(data['outcome']['status'],'blocked')
        self.assertEqual(data['outcome']['reason'],'workflow_no_progress')

    def test_missing_proof_does_not_complete_workflow(self):
        calls,data,_=self.run_workflow([result('completed',evidence=())])
        self.assertEqual(len(calls),1)
        self.assertEqual(data['outcome']['status'],'blocked')

    def test_uncertain_execution_is_not_automatically_repeated(self):
        from popot_agents.orchestrator.main import AgentRunError
        calls,data,saved=self.run_workflow([AgentRunError('response lost after publication')])
        self.assertEqual(len(calls),1)
        self.assertEqual(data['sessionId'],saved['sessionId'])
        self.assertEqual(data['outcome']['status'],'blocked')
        self.assertEqual(saved['roleConfig']['_workflow']['state'],'blocked')
        self.assertEqual(saved['roleConfig']['_workflow']['outcome']['reason'],'workflow_execution_uncertain')
        self.assertEqual(saved['roleConfig']['_workflow']['outcome']['failure'], {
            'code':'agent_run_error', 'stage':'worker_send', 'error_type':'AgentRunError',
            'childSessionIds':[], 'deadlineExpired':False})

    def test_interrupted_turn_reports_stage_and_pending_children_without_replaying(self):
        child_id='a'*16
        checkpoint={'messages':[], 'failed_delegations':[[['qa_engineer','review'],
                    {'session_id':child_id,'pending':True}]]}
        calls,data,saved=self.run_workflow([result('partial',remaining=('review',)),
            TimeoutError('workflow canceled or deadline exceeded while waiting for children')],
            checkpoint=checkpoint)
        self.assertEqual(len(calls),2)
        self.assertEqual(data['outcome']['status'],'blocked')
        self.assertEqual(data['outcome']['failure'], {
            'code':'worker_timeout', 'stage':'worker_send', 'error_type':'TimeoutError',
            'childSessionIds':[child_id], 'deadlineExpired':False})
        self.assertIn('TimeoutError at worker_send',data['outcome']['errors'])
        self.assertEqual(saved['roleConfig']['_workflow']['checkpoint'],checkpoint)

    def test_child_wait_timeout_identifies_wait_stage_and_keeps_checkpoint(self):
        class Delegation:
            waits=0
            @contextmanager
            def workflow(self,*_,**__):
                yield
            def await_children(self,*_):
                self.waits+=1
                if self.waits==2:
                    raise TimeoutError('child wait timed out')
        child_id='b'*16
        checkpoint={'messages':[], 'failed_delegations':[[['qa_engineer','review'],
                    {'session_id':child_id,'pending':True}]]}
        calls,data,saved=self.run_workflow([result('partial',remaining=('review',))],
            checkpoint=checkpoint,delegation=Delegation())
        self.assertEqual(len(calls),1)
        self.assertEqual(data['outcome']['failure'], {
            'code':'child_wait_interrupted', 'stage':'waiting_for_children', 'error_type':'TimeoutError',
            'childSessionIds':[child_id], 'deadlineExpired':False})
        self.assertEqual(data['outcome']['remaining_work'][0],
                         'Reconcile interrupted execution and external effects before continuing')
        self.assertEqual(saved['roleConfig']['_workflow']['checkpoint'],checkpoint)

    def test_os_error_reports_errno_without_exposing_exception_message(self):
        calls,data,_=self.run_workflow([OSError(13,'private path /secret/workspace')])
        self.assertEqual(len(calls),1)
        self.assertEqual(data['outcome']['failure'], {
            'code':'os_error','stage':'worker_send','error_type':'PermissionError',
            'childSessionIds':[],'deadlineExpired':False,'errno':13})
        self.assertNotIn('/secret/workspace',json.dumps(data))

    def test_workflow_turn_budget_is_enforced_even_when_receipts_change(self):
        replies=[result('partial', evidence=(f'receipt-{i}',), remaining=('more',)) for i in range(4)]
        calls,data,_=self.run_workflow(replies)
        self.assertEqual(len(calls),4)
        self.assertEqual(data['outcome']['reason'],'workflow_turn_limit')

    def test_second_autonomous_root_is_rejected_without_starting_another_worker(self):
        def while_running():
            call=request.Request(f'http://127.0.0.1:{self.server.server_port}/tasks',
                data=b'{"role":"planner","task":"second task"}',headers={'Content-Type':'application/json'})
            with self.assertRaises(error.HTTPError) as caught:request.urlopen(call,timeout=.5)
            self.assertEqual(caught.exception.code,429)
            return result('completed')
        calls,_,_=self.run_workflow([while_running])
        self.assertEqual(len(calls),1)

    def test_comment_to_running_workflow_is_queued_without_starting_another_turn(self):
        accepted = []
        def while_running():
            sid = self.server.store.list_chats()[0]['sessionId']
            call = request.Request(f'http://127.0.0.1:{self.server.server_port}/messages',
                data=json.dumps({'sessionId': sid, 'message': 'Use the new API contract',
                                 'requestId': 'comment-1'}).encode(),
                headers={'Content-Type': 'application/json'})
            with request.urlopen(call, timeout=.5) as response:
                self.assertEqual(response.status, 202)
                receipt = json.load(response)
            self.assertEqual(receipt['sessionId'], sid)
            self.assertEqual(receipt['status'], 'queued')
            with request.urlopen(call, timeout=.5) as response:
                self.assertEqual(json.load(response)['interventionId'], receipt['interventionId'])
            self.assertEqual(self.server.store.pending_interventions(sid)[0]['message'],
                             'Use the new API contract')
            with request.urlopen(f'http://127.0.0.1:{self.server.server_port}/chats/{sid}',
                                 timeout=.5) as response:
                self.assertEqual(json.load(response)['interventions'][0]['status'], 'queued')
            accepted.append(receipt)
            return {**result('completed'), 'interventionIds': [receipt['interventionId']]}
        calls, data, saved = self.run_workflow([while_running])
        self.assertEqual(len(calls), 1)
        self.assertEqual(data['outcome']['status'], 'completed')
        self.assertEqual(len(accepted), 1)
        self.assertEqual(saved['interventions'][0]['status'], 'delivered')

    def test_comment_can_target_an_active_child_session(self):
        accepted = []
        def while_running():
            sid = 'c' * 16
            self.server.store.save(sid, 'test', [], 'backend_engineer',
                {'_ax_delegation': {'state': 'TASK_STATE_WORKING'}})
            call = request.Request(f'http://127.0.0.1:{self.server.server_port}/messages',
                data=json.dumps({'sessionId': sid, 'message': 'Use the new API contract'}).encode(),
                headers={'Content-Type': 'application/json'})
            with request.urlopen(call, timeout=.5) as response:
                accepted.append(json.load(response))
                self.assertEqual(response.status, 202)
            self.assertEqual(self.server.store.pending_interventions(sid)[0]['message'],
                             'Use the new API contract')
            return result('completed')
        _, data, _ = self.run_workflow([while_running])
        self.assertEqual(data['outcome']['status'], 'completed')
        self.assertEqual(accepted[0]['sessionId'], 'c' * 16)

    def test_repeated_completed_request_returns_saved_result_without_new_execution(self):
        def repeat(first):
            url=f'http://127.0.0.1:{self.server.server_port}/tasks'
            call=request.Request(url,data=b'{"role":"planner","task":"implement","requestId":"same-operation"}',
                                 headers={'Content-Type':'application/json'})
            with request.urlopen(call,timeout=.5) as response:second=json.load(response)
            self.assertEqual(first['sessionId'],second['sessionId'])
            self.assertEqual(second['outcome']['status'],'completed')
            new_chat = request.Request(url.removesuffix('/tasks') + '/messages',
                data=b'{"role":"planner","message":"implement","requestId":"same-operation"}',
                headers={'Content-Type':'application/json'})
            with request.urlopen(new_chat, timeout=.5) as response:
                self.assertEqual(json.load(response)['sessionId'], first['sessionId'])
            other_role = request.Request(url,
                data=b'{"role":"analyst","task":"implement","requestId":"same-operation"}',
                headers={'Content-Type':'application/json'})
            with self.assertRaises(error.HTTPError) as caught:
                request.urlopen(other_role, timeout=.5)
            self.assertEqual(caught.exception.code, 409)
            changed=request.Request(url,data=b'{"role":"planner","task":"different","requestId":"same-operation"}',
                                    headers={'Content-Type':'application/json'})
            with self.assertRaises(error.HTTPError) as caught:request.urlopen(changed,timeout=.5)
            self.assertEqual(caught.exception.code,409)
        calls,_,_=self.run_workflow([result('completed')],after=repeat,request_id='same-operation')
        self.assertEqual(len(calls),1)

    def test_total_deadline_expires_even_when_remaining_work_changes(self):
        from unittest.mock import patch
        from popot_agents.orchestrator.workflow import WorkflowRun
        with patch('popot_agents.orchestrator.workflow.time.monotonic', return_value=100):
            workflow = WorkflowRun(POLICY, 'task')
            workflow.begin()
        with patch('popot_agents.orchestrator.workflow.time.monotonic', return_value=3701):
            outcome = workflow.record(result('partial', remaining=('verify',)), {'messages': []})
        self.assertEqual(outcome['outcome']['reason'], 'workflow_deadline_exhausted')

    def test_new_tool_observation_prevents_false_no_progress_stop(self):
        from popot_agents.orchestrator.workflow import WorkflowRun
        workflow = WorkflowRun(POLICY, 'task')
        for observations in (1, 2):
            workflow.begin()
            checkpoint = {'messages': [{'role': 'tool', 'content': 'CI observation'}] * observations}
            self.assertIsNone(workflow.record(result('partial', remaining=('wait for CI',)), checkpoint))
        self.assertEqual(workflow.meta['no_progress'], 1)

    def test_policy_rejects_unbounded_limits(self):
        from popot_agents.orchestrator.workflow import validate_policy
        for key in POLICY:
            with self.subTest(key=key),self.assertRaises(ValueError):validate_policy({**POLICY,key:0})


class RecoveryTests(unittest.TestCase):
    def test_recovery_never_encodes_a_cleartext_url(self):
        """Sonar S5332: the recovery hop re-enters the loopback API without a clear-text URL."""
        from pathlib import Path
        package = Path(__file__).resolve().parents[1] / 'popot_agents' / 'orchestrator'
        offenders = sorted(path.name for path in package.glob('*.py')
                           if 'http://' in path.read_text(encoding='utf-8'))
        self.assertEqual(offenders, [], 'clear-text URL literals in the orchestrator')

    def test_checkpoint_is_resumed_after_api_recreation_without_replaying_original_message(self):
        from popot_agents.orchestrator.session_store import SessionStore
        from popot_agents.orchestrator.workflow import WorkflowRun
        restored=[];calls=[];finished=threading.Event()
        class Chat:
            container_name='recovered-coordinator'
            def restore(self, messages, *, checkpoint=None):restored.append((messages,checkpoint))
            def send(self, message, **kwargs):
                calls.append(message);finished.set();return result('completed')
            def get_checkpoint(self):return {}
            def is_alive(self):return True
            def close(self):pass
        class Runner:
            def start_chat(self,sid,config,**kwargs):
                self_config=config
                if '_workflow' in self_config:raise AssertionError('private checkpoint leaked into worker config')
                return Chat()
        with tempfile.TemporaryDirectory() as directory:
            store=SessionStore(directory)
            workflow=WorkflowRun(POLICY,'original authorized task');workflow.begin()
            checkpoint={'messages':[{'role':'tool','content':'published receipt: do not repeat'}]}
            self.assertIsNone(workflow.record(result('partial',remaining=('verify',)),checkpoint))
            sid='a'*16
            store.save(sid,'test',[{'role':'user','content':'original authorized task'},
                {'role':'assistant','content':'partial'}],'backend_engineer',{'agent':'test','coordination':{'turn_seconds':120},'_workflow':workflow.meta})
            server=create_server({'test':Runner()},roles={'backend_engineer':{'agent':'test'}},store=store,
                host='127.0.0.1',port=0,workflow_policy=POLICY)
            thread=threading.Thread(target=server.serve_forever);thread.start()
            try:
                self.assertTrue(finished.wait(3),'durable checkpoint was not recovered')
                with server.session_lock(sid):saved=store.get(sid)
                self.assertEqual(saved['roleConfig']['_workflow']['state'],'completed')
                self.assertEqual(saved['roleConfig']['_workflow']['turns'],2)
                self.assertEqual(len(calls),1)
                self.assertNotEqual(calls[0],'original authorized task')
                self.assertIn('Do not repeat',calls[0])
                self.assertEqual(restored[0][1],{**checkpoint, 'run_id': workflow.meta['run_id']})
            finally:
                server.shutdown();thread.join();server.server_close()

    def test_interrupted_execution_is_blocked_without_starting_another_worker(self):
        from popot_agents.orchestrator.session_store import SessionStore
        from popot_agents.orchestrator.workflow import WorkflowRun
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            store=SessionStore(directory);workflow=WorkflowRun(POLICY,'publish');workflow.begin()
            sid='b'*16
            store.save(sid,'test',[],'planner',{'agent':'test','coordination':{},'_workflow':workflow.meta})
            runner=Mock()
            server=create_server({'test':runner},roles={'planner':{'agent':'test'}},store=store,
                host='127.0.0.1',port=0,workflow_policy=POLICY)
            try:
                server.workflow_recovery()
                meta=store.get(sid)['roleConfig']['_workflow']
                self.assertEqual(meta['state'],'blocked')
                self.assertEqual(meta['outcome']['reason'],'workflow_interrupted_execution')
                self.assertEqual(meta['outcome']['failure'], {
                    'code':'api_restart_during_execution', 'stage':'unknown',
                    'error_type':'ProcessRestart', 'childSessionIds':[]})
                runner.start_chat.assert_not_called()
            finally:server.server_close()

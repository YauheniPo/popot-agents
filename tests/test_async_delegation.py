"""Parents work between fork and join; dependency waits cannot exhaust the pool."""
import json
import threading
import time
import unittest
from unittest.mock import Mock

from tests import test_agent_reliability as reliability
from tests import test_workflow as workflows
from ax_local.tests import test_delegation as delegation
from popot_agents.orchestrator.workflow import WorkflowRun


def call(name, arguments, identifier='call'):
    return {'tool_calls':[{'id':identifier,'function':{'name':name,'arguments':json.dumps(arguments)}}]}


def pending(sid='a'*16):
    return json.dumps({'sessionId':sid,'role':'qa_engineer','state':'TASK_STATE_WORKING','pending':True,
        'outcome':{'status':'partial','evidence':[],'errors':[],'remaining_work':['working']}})


class AsyncHarnessTests(unittest.TestCase):
    def test_parent_performs_own_tool_after_fork_before_explicit_join(self):
        helper=reliability.AgentReliabilityTests()
        delegate=Mock(return_value=pending())
        _,state,bodies=helper.run_sequence([
            helper.delegate_call(),call('calculate',{'expression':'2+3'},'own'),
            call('await_tasks',{'session_ids':['a'*16],'wait_for':'all'},'join')],delegate=delegate)
        self.assertEqual(len(bodies),3)
        self.assertTrue(any(m.get('role')=='tool' and m.get('content')=='5' for m in state['messages']))
        self.assertEqual(state['await_tasks'],{'session_ids':['a'*16],'wait_for':'all'})
        self.assertEqual(state['outcome']['reason'],'child_pending')

    def test_durable_submission_needs_only_rpc_budget_not_child_execution_budget(self):
        helper=reliability.AgentReliabilityTests();delegate=Mock(return_value=pending())
        _,state,_=helper.run_sequence([helper.delegate_call(),
            call('await_tasks',{'session_ids':['a'*16]},'join'),helper.result('blocked')],delegate=delegate,timeout=60,
            run_context={'authorization':['task'],'assignments':[],'can_delegate':True})
        delegate.assert_called_once()
        self.assertEqual(state['outcome']['reason'],'child_pending')

    def test_repeated_pending_subtask_keeps_the_original_child_and_does_not_start_another(self):
        helper=reliability.AgentReliabilityTests();delegate=Mock(return_value=pending())
        _,state,_=helper.run_sequence([helper.delegate_call(),helper.delegate_call(),
            call('await_tasks',{'session_ids':['a'*16]},'join')],delegate=delegate)
        delegate.assert_called_once()
        self.assertEqual(state['failed_delegations'][0][1]['session_id'],'a'*16)
        self.assertTrue(state['failed_delegations'][0][1]['pending'])

    def test_final_answer_cannot_finish_while_a_child_is_running(self):
        helper=reliability.AgentReliabilityTests()
        _,state,bodies=helper.run_sequence([helper.delegate_call(),helper.result()],delegate=Mock(return_value=pending()))
        self.assertEqual(len(bodies),2)
        self.assertEqual(state['outcome']['reason'],'child_pending')

    def test_child_wait_does_not_consume_a_coordinator_turn(self):
        policy={**workflows.POLICY,'max_turns':1}
        run=WorkflowRun(policy,'task');run.begin()
        result={'answer':'waiting','outcome':{'status':'partial','reason':'child_pending',
            'evidence':[],'errors':[],'remaining_work':['await child']}}
        checkpoint={'messages':[],'failed_delegations':[[['qa_engineer','test'],
            {'pending':True,'session_id':'a'*16}]],'await_tasks':{'session_ids':['a'*16],'wait_for':'all'}}
        self.assertIsNone(run.record(result,checkpoint))
        run.begin()
        self.assertEqual(run.meta['turns'],1)
        self.assertIsNone(run.record(result,checkpoint))


class AsyncServiceTests(unittest.TestCase):
    setUp=delegation.DelegationTests.setUp
    send=delegation.DelegationTests.send
    wait_task=delegation.DelegationTests.wait_task

    def test_dependency_prevents_start_until_its_result_and_failure_blocks_without_a_worker(self):
        entered,release=threading.Event(),threading.Event()
        self.addCleanup(release.set)
        def callback(chat,task):
            if task=='first':entered.set();release.wait(2)
            return {'answer':'blocked','outcome':{'status':'blocked','evidence':[],
                'errors':['needs input'],'remaining_work':['resolve prerequisite']}}
        self.runner.callback=callback
        with self.service.workflow(self.parent,time.monotonic()+5,max_turns=4),self.service.turn(self.parent,5):
            first=self.send(task='first');self.assertTrue(entered.wait(1))
            second=self.service.dispatch(self.token,'backend_engineer','SendMessage',{'message':{
                'messageId':'dependent','role':'ROLE_USER','parts':[{'text':'second'}],
                'metadata':{'depends_on':[first['id']]}}})['task']
            time.sleep(.05)
            self.assertEqual(len(self.runner.chats),1)
            release.set()
            result=self.wait_task(second['id'])
            self.assertEqual(result['metadata']['outcome']['status'],'blocked')
            self.assertFalse(result['metadata']['resumable'])
            self.assertEqual(len(self.runner.chats),1)

    def test_await_any_restores_only_finished_reports_without_waiting_for_sibling(self):
        first_ready,second_release=threading.Event(),threading.Event()
        self.addCleanup(second_release.set)
        def callback(chat,task):
            if task=='first':first_ready.set()
            else:second_release.wait(2)
            return {'answer':task,'outcome':{'status':'completed','evidence':['checked'],
                'errors':[],'remaining_work':[]}}
        self.runner.callback=callback
        with self.service.workflow(self.parent,time.monotonic()+5,max_turns=4),self.service.turn(self.parent,5):
            first=self.send(task='first')
            second=self.send(task='second',message_id='second')
            self.assertTrue(first_ready.wait(1))
            class Parent:
                def get_checkpoint(inner):return {'messages':[],
                    'await_tasks':{'session_ids':[first['id'],second['id']],'wait_for':'any'},
                    'failed_delegations':[[['backend_engineer',name],{'session_id':sid,'pending':True}]
                        for sid,name in [(first['id'],'first'),(second['id'],'second')]]}
                def restore(inner,history,**options):inner.checkpoint=options['checkpoint']
            parent=Parent();started=time.monotonic()
            self.service.await_children(self.parent,parent,[],time.monotonic()+1)
            self.assertLess(time.monotonic()-started,.8)
            self.assertIn(first['id'],parent.checkpoint['completed_delegations'])
            self.assertNotIn(second['id'],parent.checkpoint['completed_delegations'])
            self.assertEqual(parent.checkpoint['failed_delegations'][0][1]['session_id'],second['id'])
            second_release.set();self.wait_task(second['id'])

    def test_backend_and_qa_run_concurrently_in_the_same_workflow(self):
        self.roles['tech_lead']['allowed_roles'].append('qa_engineer')
        entered={r:threading.Event() for r in ('backend_engineer','qa_engineer')}
        release=threading.Event();self.addCleanup(release.set)
        def callback(chat,task):
            entered[chat.config['_ax_role']].set();release.wait(2)
            return {'answer':'done','outcome':{'status':'completed'}}
        self.runner.callback=callback
        with self.service.workflow(self.parent,time.monotonic()+5,max_turns=4),self.service.turn(self.parent,5):
            tasks=[(r,self.send(role=r,message_id=r)) for r in entered]
            try:self.assertTrue(all(e.wait(.5) for e in entered.values()))
            finally:release.set()
            for role,task in tasks:self.wait_task(task['id'],role=role)

    def test_dependency_rejects_another_parent_and_self_cycle(self):
        self.runner.callback=lambda *_:{'answer':'checkpoint','outcome':{'status':'partial'}}
        from ax_local.api.delegation import A2AError
        with self.service.workflow(self.parent,time.monotonic()+5,max_turns=4),self.service.turn(self.parent,5):
            one=self.send();self.wait_task(one['id'])
            for ids in ([one['id']],['f'*16]):
                with self.subTest(ids=ids),self.assertRaises(A2AError):
                    self.service.dispatch(self.token,'backend_engineer','SendMessage',{'message':{
                        'messageId':'resume-'+ids[0],'taskId':one['id'],'role':'ROLE_USER','parts':[{'text':'continue'}],
                        'metadata':{'depends_on':ids}}})

    def test_pool_saturation_keeps_a_nested_path_available_and_cancellation_releases_every_lease(self):
        from ax_local.api.worker_queue import WorkerQueue
        from ax_local.tests.test_worker_queue import wait_for
        from ax_local.api.delegation import A2AError
        self.policy['max_depth']=3
        self.policy['max_calls_per_turn']=8
        self.roles['tech_lead']['allowed_roles']=['backend_engineer','qa_engineer']
        for name in ('analyst','code_reviewer'):
            self.roles[name]={'agent':'test','instructions':name,'tools':[],'_ax_role':name}
        for role in ('backend_engineer','qa_engineer'):self.roles[role]['allowed_roles']=['analyst']
        self.roles['analyst']['allowed_roles']=['code_reviewer']
        self.runner.worker_queue=pool=WorkerQueue(6,.3)
        root=pool.acquire('root');self.addCleanup(pool.release,'root',root)
        original=self.runner.start_chat
        peak=[1];peak_lock=threading.Lock()
        def start(sid,config,**kwargs):
            name='popot-chat-'+sid
            lease=pool.acquire(name,reservation=config['_ax_delegation']['pool_reservation'],**kwargs)
            chat=original(sid,config,**kwargs);chat.container_name=name
            close=chat.close
            def release():close();pool.release(name,lease)
            chat.close=release
            with peak_lock:peak[0]=max(peak[0],pool.status()['active'])
            return chat
        self.runner.start_chat=start
        barrier=threading.Barrier(2)
        def callback(chat,task):
            role=chat.config['_ax_role']
            if role in {'backend_engineer','qa_engineer'}:barrier.wait(1)
            target={'backend_engineer':'analyst','qa_engineer':'analyst','analyst':'code_reviewer'}.get(role)
            if target:
                token=self.service.grants[chat.session_id].token
                child=self.send(token=token,role=target,message_id=chat.session_id)
                result=self.wait_task(child['id'],token=token,role=target)
                self.assertEqual(result['metadata']['outcome']['status'],'completed')
            return {'answer':'done','outcome':{'status':'completed'}}
        self.runner.callback=callback
        with self.service.workflow(self.parent,time.monotonic()+5,max_turns=4),self.service.turn(self.parent,5):
            jobs=[(r,self.send(role=r,message_id=r)) for r in ('backend_engineer','qa_engineer')]
            for role,job in jobs:
                result=self.wait_task(job['id'],role=role)
                self.assertEqual(result['metadata']['outcome']['status'],'completed',result)
        wait_for(lambda:pool.status()['active']==1)
        self.assertLessEqual(peak[0],pool.max_workers)  # Include the full nested path without exceeding the pool.
        self.assertGreaterEqual(peak[0],4)
        self.assertEqual(pool.reservations,{})

    def test_retained_sandbox_reduces_reachable_depth_without_blocking_every_branch(self):
        from ax_local.api.worker_queue import WorkerQueue
        from ax_local.api.delegation import Job,Run
        from popot_agents.orchestrator.main import AgentRunError
        self.policy['max_depth']=5
        pool=WorkerQueue(10,1);pool.reserve('run',8)
        pool.acquire('retained',reservation='run')
        run=Run(time.monotonic()+5,pool=pool,reservation='run',reserved_slots=8)
        def job(i,depth):
            return Job(f'{i:016x}',{'_ax_delegation':{'lineage':['role']*depth}},'',run,run.deadline)
        for i in range(4):run.admitted[str(i)]=job(i,2)
        for depth in (3,4,5):
            child=job(depth+10,depth)
            self.assertTrue(self.service._can_start(child))
            run.admitted[child.session_id]=child
        with self.assertRaises(AgentRunError):self.service._can_start(job(20,6))

    def test_terminal_result_is_published_after_previous_attempt_cleanup(self):
        entered,release=threading.Event(),threading.Event();self.addCleanup(release.set)
        original=self.service._cancel_children
        def cleanup(sid):
            if sid!=self.parent:
                entered.set();release.wait(2)
            original(sid)
        self.service._cancel_children=cleanup
        self.runner.callback=lambda *_:{'answer':'checkpoint','outcome':{'status':'partial'}}
        with self.service.workflow(self.parent,time.monotonic()+5,max_turns=4),self.service.turn(self.parent,5):
            task=self.send()
            self.assertTrue(entered.wait(1))
            try:
                during=self.service.dispatch(self.token,'backend_engineer','GetTask',{'id':task['id']})
                self.assertEqual(during['status']['state'],'TASK_STATE_WORKING')
            finally:release.set()
            self.wait_task(task['id'])

    def test_cancel_during_cleanup_cannot_publish_a_successful_or_resumable_outcome(self):
        entered,release=threading.Event(),threading.Event();self.addCleanup(release.set)
        original=self.service._cancel_children
        def cleanup(sid):
            if sid!=self.parent and not entered.is_set():entered.set();release.wait(2)
            original(sid)
        self.service._cancel_children=cleanup
        self.runner.callback=lambda *_:{'answer':'checkpoint','outcome':{'status':'partial'}}
        with self.service.workflow(self.parent,time.monotonic()+5,max_turns=4),self.service.turn(self.parent,5):
            task=self.send();self.assertTrue(entered.wait(1))
            job=self.service.jobs[task['id']]
            try:
                result=self.service.dispatch(self.token,'backend_engineer','CancelTask',{'id':task['id']})
                self.assertEqual(result['metadata']['outcome']['status'],'failed')
                self.assertFalse(result['metadata']['resumable'])
            finally:release.set();job.thread.join(2)
            self.assertNotIn(task['id'],self.service.retained)

    def test_cancel_wakes_dependency_and_capacity_waiters_without_leaking_slots(self):
        from ax_local.api.worker_queue import WorkerQueue
        from ax_local.tests.test_worker_queue import wait_for
        self.runner.worker_queue=pool=WorkerQueue(4,.3)
        root=pool.acquire('root');self.addCleanup(pool.release,'root',root)
        release=threading.Event();self.addCleanup(release.set)
        original=self.runner.start_chat
        def start(sid,config,**kwargs):
            name='popot-chat-'+sid
            lease=pool.acquire(name,reservation=config['_ax_delegation']['pool_reservation'],**kwargs)
            chat=original(sid,config,**kwargs);chat.container_name=name
            close=chat.close
            def cancel():close();pool.release(name,lease);release.set()
            chat.close=cancel
            return chat
        self.runner.start_chat=start
        self.runner.callback=lambda *_: (release.wait(2),{'answer':'done','outcome':{'status':'completed'}})[1]
        with self.service.workflow(self.parent,time.monotonic()+5,max_turns=4),self.service.turn(self.parent,5):
            first=self.send();self.assertTrue(self.runner.entered.wait(1))
            second=self.send(message_id='capacity')
            third=self.service.dispatch(self.token,'backend_engineer','SendMessage',{'message':{
                'messageId':'dependency','role':'ROLE_USER','parts':[{'text':'next'}],
                'metadata':{'depends_on':[first['id']]}}})['task']
            jobs=[self.service.jobs[x['id']] for x in (first,second,third)]
        for job in jobs:
            job.thread.join(2);self.assertFalse(job.thread.is_alive())
            self.assertTrue(job.canceled.is_set())
        wait_for(lambda:pool.status()['active']==1)
        self.assertEqual(pool.reservations,{})

    def test_native_a2a_sdk_passes_dependencies_and_delivers_their_reports(self):
        from ax_local.worker.delegation import make_tools
        from ax_local.api.delegation import start_a2a_server
        reports=[]
        def callback(chat,task):
            context=self.service.turn_context(chat.session_id)
            reports.extend(context['dependency_results'])
            return {'answer':task,'outcome':{'status':'completed','evidence':['verified'],
                'errors':[],'remaining_work':[]}}
        self.runner.callback=callback
        server=start_a2a_server(self.service,'127.0.0.1',0)
        self.addCleanup(server.server_close)
        tool=make_tools({'AX_DELEGATION_URL':f'http://127.0.0.1:{server.server_port}',
            'AX_DELEGATION_TOKEN':self.token,'AX_DELEGATION_ROLES':'["backend_engineer"]'})['delegate_task']['call']
        with self.service.workflow(self.parent,time.monotonic()+5,max_turns=4),self.service.turn(self.parent,5):
            first=json.loads(tool({'role':'backend_engineer','task':'prepare'},timeout_seconds=3,call_id='prepare'))
            second=json.loads(tool({'role':'backend_engineer','task':'integrate','depends_on':[first['sessionId']]},
                timeout_seconds=3,call_id='integrate'))
            self.wait_task(second['sessionId'])
        self.assertEqual(reports[0]['sessionId'],first['sessionId'])
        self.assertEqual(reports[0]['answer'],'prepare')


class PublicationOwnershipTests(unittest.TestCase):
    def test_non_owner_cannot_publish_branch_but_can_prepare_and_test(self):
        from popot_agents.worker.delegation_policy import check_publication_owner
        from popot_agents.tools import ToolInputError
        context={'session_id':'a'*16,'publication_owner':'b'*16}
        for name,args in [('bash',{'command':'git push origin feature'}),
                ('mcp__github__push_files',{}),('mcp__github__merge_pull_request',{})]:
            with self.subTest(name=name),self.assertRaises(ToolInputError):
                check_publication_owner(name,args,context)
        check_publication_owner('bash',{'command':'git commit -m fix && python3 -m unittest'},context)
        check_publication_owner('bash',{'command':'git log --grep=push'},context)
        check_publication_owner('mcp__github__pull_request_review_write',{},context)
        check_publication_owner('bash',{'command':'git push origin feature'},
            {'session_id':'b'*16,'publication_owner':'b'*16})

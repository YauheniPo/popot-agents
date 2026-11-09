"""Delegation must enforce API-side authority and clean up child actors."""

import asyncio
import io
import json
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from email.message import Message
from types import SimpleNamespace
from unittest.mock import patch

from ax_local.api.delegation import A2AError, DelegationService, start_a2a_server
from ax_local.api.ax_runner import AxAgentRunner, AxChat
from ax_local.tests.test_worker_queue import wait_for
from popot_agents.orchestrator.main import create_server
from popot_agents.orchestrator.session_store import SessionStore
from popot_agents.progress import emit_progress, observe_progress


class FakeChat:
    def __init__(self, runner, session_id, config):
        self.runner, self.session_id, self.config = runner, session_id, config
        self.closed = False
        self.suspended = False
        self.files = {}

    def send(self, task, *, timeout_seconds=None):
        with self.runner.delegation.turn(self.session_id, timeout_seconds):
            self.runner.entered.set()
            if self.runner.callback:
                result = self.runner.callback(self, task)
                if isinstance(result, dict):
                    return result
            return {"answer": "child result: " + task}

    def after_turn(self):
        self.suspended = True

    def close(self):
        self.closed = True
        self.runner.delegation.revoke(self.session_id)


class FakeRunner:
    def __init__(self):
        self.chats = []
        self.entered = threading.Event()
        self.callback = None
        self.recovered = []

    def close_session(self, session_id):
        self.recovered.append(session_id)

    def start_chat(self, session_id, config, *, deadline=None, cancel_event=None):
        self.delegation.worker_environment(session_id, config)
        chat = FakeChat(self, session_id, config)
        self.chats.append(chat)
        return chat


class DelegationTests(unittest.TestCase):
    def test_same_revision_review_and_qa_overlap_with_reserved_capacity(self):
        from ax_local.api.worker_queue import WorkerQueue
        self.policy['max_depth']=5
        self.runner.worker_queue=pool=WorkerQueue(10,1)
        root=pool.acquire(self.parent)
        self.addCleanup(pool.release,self.parent,root)
        self.roles['code_reviewer']={**self.roles['qa_engineer'],'_ax_role':'code_reviewer'}
        self.roles['tech_lead']['allowed_roles']=['code_reviewer','qa_engineer']
        original=self.runner.start_chat
        def start(sid,config,**kwargs):
            token=pool.acquire(sid,reservation=config['_ax_delegation']['pool_reservation'])
            chat=original(sid,config,**kwargs)
            chat.container_name=sid
            close=chat.close
            def release():
                close();pool.release(sid,token)
            chat.close=release
            return chat
        self.runner.start_chat=start
        entered={r:threading.Event() for r in ('code_reviewer','qa_engineer')}
        finish=threading.Event();self.addCleanup(finish.set)
        def callback(chat,task):
            entered[chat.config['_ax_role']].set();finish.wait(2)
            return {'answer':'verified','outcome':{'status':'completed'}}
        self.runner.callback=callback
        with self.service.workflow(self.parent,time.monotonic()+5,max_turns=4,
                metadata={'turns':1,'authorization':['review only'],'run_id':'run'}),self.service.turn(self.parent,5):
            jobs=[]
            for role in entered:
                task=self.service.dispatch(self.token,role,'SendMessage',{'message':{
                    'messageId':role,'role':'ROLE_USER','parts':[{'text':'verify'}],
                    'metadata':{'revision':'a'*40,'scope_assignment':'verify'}},
                    'configuration':{'returnImmediately':True}})['task']
                jobs.append((role,task['id']))
            self.assertTrue(all(e.wait(1) for e in entered.values()))
            self.assertEqual(pool.reservations['run'],5)
            # Root + two verifiers; spare reservations remain available to nested tasks.
            self.assertEqual(pool.status()['active'],3)
            finish.set()
            for role,sid in jobs:self.wait_task(sid,role=role)
        wait_for(lambda:pool.status()['active']==1)
        self.assertEqual(pool.reservations,{})

    def test_admission_depends_on_depth_capacity_instead_of_role_or_revision(self):
        from ax_local.api.delegation import Job,Run
        from ax_local.api.worker_queue import WorkerQueue
        for slots,revision,expected in [(6,'a',True),(6,'b',True),(5,'a',False)]:
            with self.subTest(slots=slots,revision=revision):
                self.policy['max_depth']=5
                pool=WorkerQueue(10,1);pool.reserve('run',slots)
                run=Run(time.monotonic()+5,reserved_slots=slots,pool=pool,reservation='run')
                first=Job('a'*16,{'_ax_role':'code_reviewer','allowed_roles':['analyst'],'_ax_delegation':{
                    'revision':'a'*40,'lineage':['tech_lead','code_reviewer']}},'',run,run.deadline)
                second=Job('b'*16,{'_ax_role':'qa_engineer','allowed_roles':['analyst'],'_ax_delegation':{
                    'revision':revision*40,'lineage':['tech_lead','qa_engineer']}},'',run,run.deadline)
                run.admitted[first.session_id]=first
                self.assertEqual(self.service._can_start(second),expected)

    def test_continuation_preserves_prior_child_assignment_restrictions(self):
        contexts=[]
        def callback(chat,task):
            contexts.append(self.service.turn_context(chat.session_id))
            return {'answer':'checkpoint','outcome':{'status':'partial' if task=='start' else 'completed'}}
        self.runner.callback=callback
        with self.service.workflow(self.parent,time.monotonic()+5,max_turns=4,
                metadata={'turns':1,'authorization':['authorized scope'],'run_id':'run'}),self.service.turn(self.parent,5):
            first=self.service.dispatch(self.token,'backend_engineer','SendMessage',{'message':{
                'messageId':'start','role':'ROLE_USER','parts':[{'text':'start'}],
                'metadata':{'scope_assignment':'Inspect only. Do not push.'}}})['task']
            self.wait_task(first['id'])
            self.service.dispatch(self.token,'backend_engineer','SendMessage',{'message':{
                'messageId':'finish','taskId':first['id'],'role':'ROLE_USER','parts':[{'text':'finish'}],
                'metadata':{'scope_assignment':'Finish verification.'}}})
            self.wait_task(first['id'])
        self.assertEqual(contexts[-1]['assignments'],['Inspect only. Do not push.','Finish verification.'])

    def test_partial_child_resumes_same_sandbox_and_preserves_transcript(self):
        def callback(chat, task):
            if task == 'implement':
                chat.files['work.txt'] = 'verified state'
                return {'answer': 'Tests remain to run',
                        'outcome': {'status': 'partial', 'reason': 'work_budget_exhausted'}}
            self.assertEqual(chat.files['work.txt'], 'verified state')
            return {'answer': 'Checks passed', 'outcome': {'status': 'completed'}}
        self.runner.callback = callback
        with self.service.turn(self.parent, 5):
            first = self.wait_task(self.send()['id'])
            self.assertEqual(first['metadata']['outcome']['status'], 'partial')
            self.assertTrue(first['metadata']['resumable'])
            self.assertTrue(self.runner.chats[0].suspended)
            self.assertFalse(self.runner.chats[0].closed)
            params = {'message': {'messageId': 'continue-1', 'role': 'ROLE_USER',
                                  'taskId': first['id'], 'parts': [{'text': 'Run the remaining checks'}]}}
            resumed = self.service.dispatch(self.token, 'backend_engineer', 'SendMessage', params)['task']
            self.assertEqual(resumed['id'], first['id'])
            result = self.wait_task(first['id'])
            duplicate = self.service.dispatch(self.token, 'backend_engineer', 'SendMessage', params)['task']
            self.assertEqual(duplicate['id'], first['id'])
            self.assertEqual(self.service.grants[self.parent].run.calls, 2)
        self.assertEqual(result['metadata']['outcome']['status'], 'completed')
        self.assertEqual(len(self.runner.chats), 1)
        self.assertEqual(len(self.store.get(first['id'])['messages']), 4)
        self.assertTrue(self.runner.chats[0].closed)

    def test_continuation_enforces_parent_role_call_limit_and_missing_checkpoint(self):
        self.runner.callback = lambda *_: {'answer': 'Incomplete', 'outcome': {'status': 'partial'}}
        with self.service.turn(self.parent, 5):
            first = self.wait_task(self.send()['id'])
            params = {'message': {'messageId': 'continue', 'role': 'ROLE_USER',
                                  'taskId': first['id'], 'parts': [{'text': 'Continue'}]}}
            other = '1111111111111111'
            env = self.service.worker_environment(other, self.roles['tech_lead'])
            with self.service.turn(other, 5), self.assertRaisesRegex(A2AError, 'not found'):
                self.service.dispatch(env['AX_DELEGATION_TOKEN'], 'backend_engineer', 'SendMessage', params)
            self.roles['tech_lead']['allowed_roles'].append('qa_engineer')
            with self.assertRaisesRegex(A2AError, 'not found'):
                self.service.dispatch(self.token, 'qa_engineer', 'SendMessage', params)
            with patch.dict(self.policy, {'max_calls_per_turn': 1}), self.assertRaisesRegex(A2AError, 'call limit'):
                self.service.dispatch(self.token, 'backend_engineer', 'SendMessage', params)
            chat, touched = self.service.retained.pop(first['id'])
            with self.assertRaisesRegex(A2AError, 'empty replacement'):
                self.service.dispatch(self.token, 'backend_engineer', 'SendMessage', params)
            chat.close()
        self.assertEqual(len(self.runner.chats), 1)

    def test_concurrent_continuation_has_one_execution_and_duplicate_is_idempotent(self):
        entered, finish = threading.Event(), threading.Event()
        self.addCleanup(finish.set)
        def callback(chat, task):
            if task != 'implement':
                entered.set()
                finish.wait(2)
            return {'answer': 'Incomplete', 'outcome': {'status': 'partial'}}
        self.runner.callback = callback
        with self.service.turn(self.parent, 5):
            first = self.wait_task(self.send()['id'])
            params = {'message': {'messageId': 'continue-1', 'role': 'ROLE_USER',
                                  'taskId': first['id'], 'parts': [{'text': 'Continue'}]}}
            self.service.dispatch(self.token, 'backend_engineer', 'SendMessage', params)
            self.assertTrue(entered.wait(1))
            duplicate = self.service.dispatch(self.token, 'backend_engineer', 'SendMessage', params)['task']
            self.assertEqual(duplicate['id'], first['id'])
            params['message']['messageId'] = 'continue-2'
            with self.assertRaisesRegex(A2AError, 'already active'):
                self.service.dispatch(self.token, 'backend_engineer', 'SendMessage', params)
            self.assertEqual(self.service.grants[self.parent].run.calls, 2)
            finish.set()
            self.wait_task(first['id'])
        self.assertEqual(len(self.runner.chats), 1)

    def test_native_sdk_continuation_reuses_child_session(self):
        from ax_local.worker.delegation import make_tools
        self.runner.callback = lambda chat, task: {
            'answer': task, 'outcome': {'status': 'partial' if task == 'start' else 'completed'}}
        server = start_a2a_server(self.service, '127.0.0.1', 0)
        self.addCleanup(server.server_close)
        env = self.service.worker_environment(self.parent, self.roles['tech_lead'])
        env['AX_DELEGATION_POLL_SECONDS'] = '0.01'
        tool = make_tools(env)['delegate_task']['call']
        with self.service.turn(self.parent, 5):
            first = json.loads(tool({'role': 'backend_engineer', 'task': 'start'},
                                    timeout_seconds=3, call_id='start'))
            self.assertEqual(first['outcome']['status'], 'partial')
            second = json.loads(tool({'role': 'backend_engineer', 'session_id': first['sessionId'],
                                      'task': 'finish'}, timeout_seconds=3, call_id='continue'))
        self.assertEqual(second.get('outcome', {}).get('status'), 'completed', second)
        self.assertEqual(second['sessionId'], first['sessionId'])
        self.assertEqual(len(self.runner.chats), 1)

    def test_parent_can_continue_child_in_a_later_turn_until_checkpoint_expires(self):
        self.runner.callback = lambda *_: {'answer': 'Incomplete', 'outcome': {'status': 'partial'}}
        with self.service.turn(self.parent, 5):
            first = self.wait_task(self.send()['id'])
        with self.service.turn(self.parent, 5):
            params = {'message': {'messageId': 'later-turn', 'role': 'ROLE_USER',
                                  'taskId': first['id'], 'parts': [{'text': 'Continue'}]}}
            self.service.dispatch(self.token, 'backend_engineer', 'SendMessage', params)
            self.assertTrue(self.wait_task(first['id'])['metadata']['resumable'])
            chat, _ = self.service.retained[first['id']]
            self.service.retained[first['id']] = (chat, time.monotonic() - 10000)
            self.service.maintenance()
            self.assertTrue(chat.closed)
            self.assertFalse(self.wait_task(first['id'])['metadata']['resumable'])
            params['message']['messageId'] = 'expired-turn'
            with self.assertRaisesRegex(A2AError, 'empty replacement'):
                self.service.dispatch(self.token, 'backend_engineer', 'SendMessage', params)
        self.assertEqual(len(self.runner.chats), 1)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runner = FakeRunner()
        self.roles = {name: {"agent": "test", "instructions": name, "tools": [],
                             "_ax_role": name}
                      for name in ("tech_lead", "backend_engineer", "qa_engineer")}
        self.roles["tech_lead"]["allowed_roles"] = ["backend_engineer"]
        self.roles["backend_engineer"]["allowed_roles"] = ["qa_engineer", "tech_lead"]
        self.policy = {"enabled": True, "port": 8005, "max_depth": 2,
                       "max_calls_per_turn": 4,
                       "turn_timeout_seconds": 300, "poll_interval_seconds": 0.01}
        self.store = SessionStore(self.temporary.name)
        self.service = DelegationService(self.roles, self.policy, {"test": self.runner}, self.store,
                                         "http://127.0.0.1:8005")
        self.runner.delegation = self.service
        self.addCleanup(self.service.close)
        self.parent = "0123456789abcdef"
        env = self.service.worker_environment(self.parent, self.roles["tech_lead"])
        self.token = env["AX_DELEGATION_TOKEN"]

    def send(self, token=None, role="backend_engineer", message_id="message-1", task="implement"):
        return self.service.dispatch(token or self.token, role, "SendMessage", {
            "message": {"messageId": message_id, "role": "ROLE_USER",
                        "parts": [{"text": task}]},
            "configuration": {"returnImmediately": True},
        })["task"]

    def wait_task(self, task_id, token=None, role="backend_engineer"):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            task = self.service.dispatch(token or self.token, role, "GetTask", {"id": task_id})
            if task["status"]["state"] not in {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}:
                return task
            time.sleep(0.01)
        self.fail("child did not finish")

    def test_delegates_and_saves_child_transcript_and_parent_link(self):
        with self.service.turn(self.parent, 5):
            task = self.wait_task(self.send()["id"])
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(task["artifacts"][0]["parts"][0]["text"], "child result: implement")
        saved = self.store.get(task["id"])
        self.assertEqual(saved["role"], "backend_engineer")
        self.assertEqual(saved["roleConfig"]["_ax_delegation"]["parent_session_id"], self.parent)
        self.assertEqual(saved["messages"][-1]["content"], "child result: implement")
        self.assertTrue(self.runner.chats[0].closed)
        self.assertNotIn(self.token, str(saved))

    def test_root_call_id_reaches_delegated_session(self):
        with self.service.workflow(self.parent,time.monotonic()+5,max_turns=4,
                metadata={'turns':1,'request_id':'call-root','run_id':'run'}), \
                self.service.turn(self.parent,5):
            task=self.wait_task(self.send()['id'])
        saved=self.store.get(task['id'])
        self.assertEqual(saved['roleConfig']['_ax_delegation']['root_call_id'],'call-root')
        self.assertEqual(saved['roleConfig']['_ax_delegation']['root_session_id'],self.parent)

    def test_last_coordinator_turn_rejects_child_before_startup(self):
        with self.service.workflow(self.parent,time.monotonic()+30,max_turns=4,
                                   metadata={'turns':4,'original_task':'fix'}):
            with self.service.turn(self.parent,5):
                with self.assertRaisesRegex(A2AError,'last.*turn|acceptance'):
                    self.send()
        self.assertEqual(self.runner.chats,[])

    def test_server_scope_does_not_inherit_generated_continuations(self):
        authority=['Original task; do not publish']
        with self.service.workflow(self.parent,time.monotonic()+30,max_turns=4,
            metadata={'turns':1,'authorization':authority,'original_task':authority[0]}):
            with self.service.turn(self.parent,5):
                context=self.service.turn_context(self.parent)
                self.assertEqual(context['authorization'],authority)
                self.assertEqual(context['assignments'],[])
                self.assertEqual(context['role'],'tech_lead')

    def test_reviewer_handoff_assigns_backend_publication_owner(self):
        reviewer = 'f' * 16
        self.roles['code_reviewer'] = {'agent': 'test', 'instructions': 'review',
                                       'tools': [], '_ax_role': 'code_reviewer',
                                       'allowed_roles': ['backend_engineer']}
        token = self.service.worker_environment(reviewer, self.roles['code_reviewer'])['AX_DELEGATION_TOKEN']
        with self.service.workflow(reviewer, time.monotonic() + 5, max_turns=4), self.service.turn(reviewer, 5):
            self.assertIsNone(self.service.turn_context(reviewer)['publication_owner'])
            child = self.send(token=token, task='fix approved finding')
            self.assertEqual(self.service.turn_context(reviewer)['publication_owner'], child['id'])
            self.wait_task(child['id'], token=token)

    def test_child_tool_observation_is_persisted_before_its_turn_finishes(self):
        from popot_agents.work_evidence import observe_tool_result
        entered, release = threading.Event(), threading.Event()
        def callback(chat, task):
            emit_progress('work_evidence', receipt=observe_tool_result('bash', {'command':'git push'},
                'exit_code=0\n old..new HEAD -> fix\n', 'push'))
            entered.set()
            release.wait(3)
        self.runner.callback = callback
        try:
            with self.service.turn(self.parent, 5):
                task = self.send()
                self.assertTrue(entered.wait(1))
                saved = self.store.get(task['id'])
                self.assertEqual(saved['roleConfig']['_work_evidence'][0]['kind'], 'publication')
                self.assertNotIn('outcome', saved['roleConfig']['_ax_delegation'])
                release.set()
                self.wait_task(task['id'])
                self.assertEqual(len(self.store.get(task['id'])['roleConfig']['_work_evidence']), 1)
        finally:
            release.set()

    def test_child_deadline_uses_call_budget_and_cannot_extend_parent_deadline(self):
        remaining = []
        self.runner.callback = lambda chat, _: remaining.append(
            self.service.grants[chat.session_id].deadline - time.monotonic())
        with self.service.turn(self.parent, 50):
            for index, budget in enumerate((2, 500)):
                task = self.service.dispatch(self.token, 'backend_engineer', 'SendMessage', {
                    'message': {'messageId': f'budget-{index}', 'role': 'ROLE_USER',
                                'parts': [{'text': 'review'}], 'metadata': {'timeout_seconds': budget}},
                    'configuration': {'returnImmediately': True},
                })['task']
                self.assertEqual(self.wait_task(task['id'])['status']['state'], 'TASK_STATE_COMPLETED')
        self.assertGreater(remaining[0], 0)
        self.assertLessEqual(remaining[0], 2)
        self.assertLessEqual(remaining[1], 50)

    def test_invalid_call_budget_does_not_start_a_child(self):
        with self.service.turn(self.parent, 50):
            for budget in (0, -1, True, '2', float('nan'), float('inf')):
                with self.subTest(budget=budget), self.assertRaisesRegex(A2AError, 'timeout_seconds'):
                    self.service.dispatch(self.token, 'backend_engineer', 'SendMessage', {
                        'message': {'messageId': 'bad-budget', 'role': 'ROLE_USER',
                                    'parts': [{'text': 'review'}], 'metadata': {'timeout_seconds': budget}},
                        'configuration': {'returnImmediately': True},
                    })
        self.assertFalse(self.runner.chats)

    def test_child_uses_its_role_profile_instead_of_the_parent_provider(self):
        child_runner = FakeRunner()
        child_runner.delegation = self.service
        self.service.runners['child_provider'] = child_runner
        self.roles['backend_engineer']['agent'] = 'child_provider'
        with self.service.turn(self.parent, 5):
            task = self.wait_task(self.send()['id'])
        self.assertEqual(task['status']['state'], 'TASK_STATE_COMPLETED')
        self.assertEqual(self.runner.chats, [])
        self.assertEqual(len(child_runner.chats), 1)
        self.assertTrue(child_runner.chats[0].closed)
        self.assertEqual(self.store.get(task['id'])['agent'], 'child_provider')

    def test_nested_child_progress_reaches_parent_before_final_answer_via_a2a(self):
        from ax_local.worker.delegation import make_tools
        received, completed = [], threading.Event()
        server = start_a2a_server(self.service, '127.0.0.1', 0)
        self.addCleanup(server.server_close)

        def delegate(session_id, config, role):
            env = self.service.worker_environment(session_id, config)
            env['AX_DELEGATION_POLL_SECONDS'] = '0.01'
            tools = make_tools(env)
            return json.loads(tools['delegate_task']['call'](
                {'role': role, 'task': 'inspect'}, timeout_seconds=5, call_id=session_id + '-inspect'))

        def notify(message):
            received.append(message)
            if json.loads(message).get('agent_role') == 'qa_engineer' and json.loads(message).get('name') == 'mcp__github__pull_request_read':
                completed.set()

        def callback(chat, _):
            emit_progress('model_request')
            if chat.config['_ax_role'] == 'backend_engineer':
                result = delegate(chat.session_id, chat.config, 'qa_engineer')
                self.assertEqual(result.get('state'), 'TASK_STATE_COMPLETED', result)
            else:
                emit_progress('tool_call', name='mcp__github__pull_request_read',
                              arguments={'token': 'PRIVATE'})
                if not completed.wait(2):
                    raise RuntimeError('child progress was not received before completion')

        self.runner.callback = callback
        with self.service.turn(self.parent, 10), observe_progress(notify):
            result = delegate(self.parent, self.roles['tech_lead'], 'backend_engineer')
        self.assertTrue(completed.is_set(), received)
        self.assertEqual(result.get('state'), 'TASK_STATE_COMPLETED', result)
        self.assertEqual(sum(json.loads(m).get('agent_role') == 'qa_engineer' and json.loads(m).get('name') == 'mcp__github__pull_request_read' for m in received), 1)
        events = [json.loads(message) for message in received]
        self.assertTrue(any(event.get('event') == 'model_request' and
            event.get('agent_role') == 'backend_engineer' and
            event.get('agent_session_id') == self.runner.chats[0].session_id for event in events))
        self.assertTrue(any(event.get('name') == 'mcp__github__pull_request_read' and
            event.get('agent_session_id') == self.runner.chats[1].session_id for event in events))
        self.assertNotIn('PRIVATE', json.dumps(received))
        wait_for(lambda: not self.service.jobs)
        self.assertTrue(all(chat.closed for chat in self.runner.chats))

    def test_child_activity_is_bounded_and_cannot_be_read_by_another_parent(self):
        ready, finish = threading.Event(), threading.Event()
        self.addCleanup(finish.set)
        def callback(*_):
            for _ in range(100):
                emit_progress('model_request')
                emit_progress('tool_call', name='read_file', arguments={'path': 'app.py', 'token': 'PRIVATE'})
            ready.set()
            finish.wait(3)
        self.runner.callback = callback
        other = '1111111111111111'
        other_env = self.service.worker_environment(other, self.roles['tech_lead'])
        with self.service.turn(self.parent, 5), self.service.turn(other, 5):
            task_id = self.send()['id']
            self.assertTrue(ready.wait(2))
            task = self.service.dispatch(self.token, 'backend_engineer', 'GetTask', {'id': task_id})
            activity = task['metadata'].get('activity', [])
            self.assertEqual(len(activity), 64)
            self.assertTrue(all(json.loads(a['message']).get('agent_role') == 'backend_engineer' for a in activity))
            self.assertEqual([a['seq'] for a in activity], sorted(set(a['seq'] for a in activity)))
            with self.assertRaises(A2AError):
                self.service.dispatch(other_env['AX_DELEGATION_TOKEN'], 'backend_engineer',
                                      'GetTask', {'id': task_id})
            finish.set()
            task = self.wait_task(task_id)
            self.assertEqual(task['status']['state'], 'TASK_STATE_COMPLETED')
            self.assertIn('activity', task['metadata'])
            self.assertNotIn('PRIVATE', json.dumps(task))

    def test_large_progress_does_not_hide_completed_child_result_over_a2a(self):
        from ax_local.worker.delegation import make_tools
        from popot_agents.runtime_config import RUNTIME

        server = start_a2a_server(self.service, '127.0.0.1', 0)
        self.addCleanup(server.server_close)
        env = self.service.worker_environment(self.parent, self.roles['tech_lead'])
        env['AX_DELEGATION_POLL_SECONDS'] = '0.01'
        delegate = make_tools(env)['delegate_task']['call']
        received = []

        def callback(*_):
            for index in range(60):
                emit_progress('assistant_reasoning', content='x' * 10000, index=index)
            emit_progress('assistant_reasoning', content=('🔍\\"\n' * 100000))

        self.runner.callback = callback
        with self.service.turn(self.parent, 10), observe_progress(received.append):
            result = json.loads(delegate({'role': 'backend_engineer', 'task': 'inspect'},
                                         timeout_seconds=8, call_id='large-progress'))
        self.assertEqual(result.get('state'), 'TASK_STATE_COMPLETED', result)
        self.assertFalse(result.get('isError'), result)
        self.assertEqual(result['answer'], 'child result: inspect')
        self.assertTrue(any(json.loads(item).get('truncated') for item in received))
        saved = self.store.get(result['sessionId'])
        activity = saved['roleConfig']['_ax_delegation']['activity']
        self.assertLessEqual(len(json.dumps(activity).encode()),
                             min(65536, RUNTIME['limits']['history_bytes'] // 4))
        self.assertTrue(all(len(json.dumps(item).encode()) <= 16384 for item in activity))
        self.assertEqual(activity[-1]['seq'], 63)  # Start, 61 harness events, completion.
        self.assertEqual([item['seq'] for item in activity], sorted({item['seq'] for item in activity}))

    def test_persisted_progress_leaves_room_for_a_large_task_result(self):
        from google.protobuf.json_format import MessageToDict, ParseDict
        from a2a.types import a2a_pb2 as types
        from popot_agents.runtime_config import RUNTIME

        with self.service.turn(self.parent, 5):
            task_id = self.wait_task(self.send()['id'])['id']
            wait_for(lambda: not self.service.jobs)
            saved = self.store.get(task_id)
            meta = saved['roleConfig']['_ax_delegation']
            # Simulate a stored Task from before byte limits, including JSON
            # escaping and Unicode expansion beyond plain text length.
            meta['activity'] = [{'seq': index, 'message': json.dumps({
                'event': 'tool_result', 'agent_role': 'qa_engineer',
                'output': '🧪\\"\n' * 5000}, ensure_ascii=False)} for index in range(1, 65)]
            meta['answer'] = 'verified ' * 55000
            self.store.save(task_id, saved['agent'], saved['messages'], saved['role'], saved['roleConfig'])
            task = self.service.dispatch(self.token, 'backend_engineer', 'GetTask', {'id': task_id})
            wire = MessageToDict(ParseDict(task, types.Task()))
            payload = json.dumps({'jsonrpc': '2.0', 'id': 'response-id', 'result': {'task': wire}}).encode()
            self.assertLessEqual(len(payload), RUNTIME['limits']['history_bytes'] + 4096)
            self.assertEqual(task['artifacts'][0]['parts'][0]['text'], meta['answer'])
            # Returned progress is reduced without changing the saved answer.
            self.assertEqual(self.store.get(task_id)['roleConfig']['_ax_delegation']['answer'], meta['answer'])

    def test_progress_with_unicode_and_escaping_obeys_a_custom_small_byte_budget(self):
        from ax_local.api.delegation import Job, Run
        from popot_agents.runtime_config import RUNTIME

        run = Run(time.monotonic() + 5)
        job = Job('f' * 16, {'_ax_role': 'backend_engineer'}, '', run, run.deadline)
        with patch.dict(RUNTIME['limits'], {'history_bytes': 8192}):
            for index in range(10):
                self.service._record_progress(job, json.dumps({
                    'event': 'assistant_reasoning', 'agent_role': 'qa_engineer', 'agent_session_id': 'd'*16,
                    'reasoning_details': [{'text': '🧪\\"\n' * 5000}], 'index': index}, ensure_ascii=False))
        self.assertTrue(job.activity)
        self.assertLessEqual(len(json.dumps(list(job.activity)).encode()), 2048)
        self.assertEqual(job.activity[-1]['seq'], 10)
        event = json.loads(job.activity[-1]['message'])
        self.assertEqual(event['agent_role'], 'qa_engineer')
        self.assertEqual(event['agent_session_id'], 'd'*16)
        self.assertTrue(event['truncated'])


    def test_denies_unavailable_role_even_if_request_claims_other_parent(self):
        with self.service.turn(self.parent, 5), self.assertRaisesRegex(A2AError, "not allowed"):
            self.send(role="qa_engineer")
        self.assertEqual(self.runner.chats, [])

    def test_capability_is_invalid_outside_parent_turn_or_after_revoke(self):
        with self.assertRaises(A2AError):
            self.send()
        self.service.revoke(self.parent)
        with self.assertRaises(A2AError):
            self.send()

    def test_repeated_message_id_does_not_spawn_another_child(self):
        with self.service.turn(self.parent, 5):
            first = self.wait_task(self.send()["id"])
            second = self.send()
            self.assertEqual(first["id"], second["id"])
            with self.assertRaisesRegex(A2AError, "different"):
                self.send(task="changed input")
        self.assertEqual(len(self.runner.chats), 1)

    def test_workflow_child_survives_parent_turn_and_finishes_before_parent_resumes(self):
        entered, release = threading.Event(), threading.Event()
        def callback(chat, task):
            entered.set()
            release.wait(2)
            return {'answer':'child checked', 'outcome':{'status':'completed',
                'evidence':['child receipt'], 'errors':[], 'remaining_work':[]}}
        self.runner.callback = callback
        with self.service.workflow(self.parent, time.monotonic()+5, max_turns=4):
            with self.service.turn(self.parent, .1):
                task=self.send(); self.assertTrue(entered.wait(1))
                job=self.service.jobs[task['id']]
                self.assertGreater(job.deadline, self.service.grants[self.parent].deadline)
                self.assertTrue(task['metadata']['durable'])
            self.assertFalse(job.canceled.is_set())
            release.set()
            class Parent:
                def get_checkpoint(inner):
                    return {'messages':[{'role':'tool','content':json.dumps({'sessionId':task['id'],'pending':True})}],
                        'failed_delegations':[[['backend_engineer','implement'],{'session_id':task['id'],'pending':True}]],
                        'outcome':{'status':'partial'}}
                def restore(inner, history, **options): inner.checkpoint=options['checkpoint']
            parent=Parent()
            self.assertTrue(self.service.await_children(self.parent,parent,[],time.monotonic()+3))
            self.assertEqual(parent.checkpoint['failed_delegations'],[])
            self.assertEqual(parent.checkpoint['completed_delegations'][task['id']]['outcome']['status'],'completed')
            self.assertEqual(json.loads(parent.checkpoint['messages'][0]['content'])['state'],'TASK_STATE_COMPLETED')

    def wait_task_internal(self, sid):
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            saved=self.store.get(sid)
            if saved['roleConfig']['_ax_delegation']['state'] not in {'TASK_STATE_SUBMITTED','TASK_STATE_WORKING'}:
                return self.service._task(saved)
            time.sleep(.01)
        self.fail('durable task did not finish')

    def test_server_delivered_blocker_updates_pending_failure_status(self):
        self.runner.callback = lambda *_: {'answer':'Need a decision', 'outcome':{
            'status':'blocked','evidence':[],'errors':[],'remaining_work':['User decision']}}
        with self.service.workflow(self.parent,time.monotonic()+5,max_turns=4):
            with self.service.turn(self.parent,.1): task=self.send()
            class Parent:
                def get_checkpoint(inner): return {'failed_delegations':[[
                    ['backend_engineer','implement'], {'session_id':task['id'],
                        'pending':True,'status':'partial'}]]}
                def restore(inner, history, **options): inner.checkpoint=options['checkpoint']
            parent=Parent()
            self.service.await_children(self.parent,parent,[],time.monotonic()+3)
            failure=parent.checkpoint['failed_delegations'][0][1]
            self.assertEqual(failure['status'],'blocked')
            self.assertFalse(failure['pending'])

    def test_durable_workflow_end_cancels_parallel_children(self):
        entered,release=threading.Event(),threading.Event()
        def callback(chat,task):
            entered.set();release.wait(2)
            return {'answer':'done'}
        self.runner.callback=callback
        try:
            with self.service.workflow(self.parent,time.monotonic()+5,max_turns=2):
                with self.service.turn(self.parent,.1):
                    one=self.send(message_id='one');self.assertTrue(entered.wait(1))
                    two=self.send(message_id='two')
                wait_for(lambda:len(self.runner.chats)==2)
                jobs=[self.service.jobs[one['id']],self.service.jobs[two['id']]]
            self.assertTrue(all(job.canceled.is_set() for job in jobs))
        finally:release.set()
        for job in jobs:job.thread.join(2)
        self.assertTrue(all(chat.closed for chat in self.runner.chats))

    def test_measured_mcp_setup_replaces_cold_start_estimate(self):
        from ax_local.api.delegation import Job, Run
        role=self.roles['backend_engineer'];role['mcpServers']={'github':{},'fetch':{}}
        cold=self.service.setup_estimate(role)
        run=Run(time.monotonic()+5)
        job=Job('a'*16,role,'',run,run.deadline)
        self.service._record_progress(job,json.dumps({'event':'mcp_discovery_completed',
            'server':'github','elapsed_seconds':2}))
        self.service._record_progress(job,json.dumps({'event':'mcp_discovery_completed',
            'server':'fetch','elapsed_seconds':1}))
        self.assertLess(self.service.setup_estimate(role),cold)
        self.assertGreaterEqual(self.service.setup_estimate(role),3)

    def test_durable_admission_uses_measured_setup_and_preserves_call_budget(self):
        role = self.roles['backend_engineer']
        role['mcpServers'] = {'github': {}, 'fetch': {}}
        metadata = {'delegationCalls': 3}
        with self.service.workflow(self.parent, time.monotonic()+200, max_turns=1, metadata=metadata):
            with self.service.turn(self.parent, 1):
                with self.assertRaisesRegex(A2AError, 'setup'):
                    self.send()
                self.service.discovery_samples[('backend_engineer','github')] = [2]
                self.service.discovery_samples[('backend_engineer','fetch')] = [1]
                self.wait_task_internal(self.send()['id'])
                self.assertEqual(metadata['delegationCalls'], 4)
                with self.assertRaisesRegex(A2AError, 'limit'):
                    self.send(message_id='over-budget')

    def test_single_api_request_resumes_same_parent_after_longer_child(self):
        from urllib import request
        service = self.service
        calls = []
        class Parent(FakeChat):
            container_name = 'test-parent'
            checkpoint = {}
            def is_alive(inner): return True
            def get_checkpoint(inner): return inner.checkpoint
            def restore(inner, history, **options): inner.checkpoint = options.get('checkpoint', {})
            def send(inner, message, *, timeout_seconds=None):
                with service.turn(inner.session_id, timeout_seconds):
                    calls.append(message)
                    if len(calls) == 1:
                        token = service.grants[inner.session_id].token
                        task = self.send(token=token, task='long child')
                        inner.checkpoint = {'messages': [{'role':'tool', 'content':json.dumps(
                            {'sessionId':task['id'], 'pending':True})}], 'failed_delegations':[
                                [['backend_engineer','long child'], {'session_id':task['id'],'pending':True}]]}
                        return {'answer':'waiting', 'outcome':{'status':'partial', 'reason':'child_pending',
                            'evidence':[], 'errors':[], 'remaining_work':['Await child']}}
                    self.assertEqual(len(inner.checkpoint['completed_delegations']), 1)
                    return {'answer':'verified child', 'outcome':{'status':'completed',
                        'evidence':['child receipt verified'], 'errors':[], 'remaining_work':[]}}
        original_start = self.runner.start_chat
        def start(sid, config, **kwargs):
            if config['_ax_role'] != 'tech_lead': return original_start(sid, config, **kwargs)
            service.worker_environment(sid, config)
            return Parent(self.runner, sid, config)
        def child(*_):
            time.sleep(1.1)
            return {'answer':'checked', 'outcome':{'status':'completed',
                'evidence':['child verified'], 'errors':[], 'remaining_work':[]}}
        self.runner.callback = child
        server = create_server({'test':self.runner}, roles=self.roles, session_dir=self.temporary.name,
            host='127.0.0.1', port=0, workflow_policy={'max_turns':4,'total_seconds':5,
                'turn_seconds':1,'max_no_progress':2,'max_concurrent':1})
        service.store = server.store
        thread = threading.Thread(target=server.serve_forever); thread.start()
        try:
            with patch.object(self.runner, 'start_chat', side_effect=start):
                call = request.Request(f'http://127.0.0.1:{server.server_port}/tasks',
                    data=json.dumps({'role':'tech_lead','task':'coordinate'}).encode(),
                    headers={'Content-Type':'application/json'})
                with request.urlopen(call, timeout=4) as response: result = json.load(response)
            self.assertEqual(result['outcome']['status'], 'completed')
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(self.runner.chats), 1)
            saved = server.store.get(result['sessionId'])
            self.assertEqual(saved['roleConfig']['_workflow']['delegationCalls'], 1)
        finally:
            server.shutdown();thread.join();server.server_close()

    def test_partial_receipts_are_persisted_while_child_is_still_working(self):
        entered, release = threading.Event(), threading.Event()
        count = []
        def callback(chat, task):
            count.append(task)
            if len(count) == 1:
                return {'answer': 'Commit published; QA pending', 'outcome': {'status': 'partial',
                    'evidence': ['published commit'], 'remaining_work': ['QA'], 'errors': []}}
            entered.set()
            release.wait(3)
            return {'answer': 'QA passed', 'outcome': {'status': 'completed',
                    'evidence': ['QA passed'], 'remaining_work': [], 'errors': []}}
        self.runner.callback = callback
        with patch.object(FakeChat, 'get_checkpoint', return_value={'messages': []}, create=True), \
             self.service.turn(self.parent, 5):
            task = self.send()
            try:
                self.assertTrue(entered.wait(1))
                saved = self.store.get(task['id'])
                meta = saved['roleConfig']['_ax_delegation']
                self.assertEqual(meta['state'], 'TASK_STATE_WORKING')
                self.assertEqual(meta['outcome']['evidence'], ['published commit'])
                self.assertTrue(meta['outcomeRecordedAt'])
                self.assertEqual(meta['workflow']['turns'], 2)
            finally:
                release.set()
            self.assertEqual(self.wait_task(task['id'])['metadata']['outcome']['status'], 'completed')

    def test_child_failure_after_partial_preserves_evidence_for_parent_reconciliation(self):
        calls = []
        def callback(chat, task):
            calls.append(task)
            if len(calls) == 1:
                return {'answer': 'Read CI logs; checks remain', 'outcome': {'status': 'partial',
                    'evidence': ['CI logs read in this sandbox'], 'errors': [],
                    'remaining_work': ['verify the patch']}}
            from popot_agents.orchestrator.main import AgentRunError
            raise AgentRunError('MCP server fetch timed out during discovery')
        self.runner.callback = callback
        with patch.object(FakeChat, 'get_checkpoint', return_value={'messages': []}, create=True), \
             self.service.turn(self.parent, 5):
            task = self.wait_task(self.send()['id'])
        self.assertEqual(task['status']['state'], 'TASK_STATE_FAILED')
        outcome = task['metadata']['outcome']
        self.assertEqual(outcome['status'], 'failed')
        self.assertIn('Read CI logs; checks remain', outcome.get('last_partial_answer', ''))
        self.assertIn('CI logs read in this sandbox', outcome.get('evidence', []))
        self.assertIn('verify the patch', outcome.get('remaining_work', []))
        self.assertIn('MCP server fetch timed out during discovery', outcome.get('errors', []))

    def test_child_yields_checkpoint_when_parent_budget_cannot_cover_mcp_rediscovery(self):
        calls = []
        self.roles['backend_engineer']['mcpServers'] = {'fetch': {}}
        def callback(chat, task):
            calls.append(task)
            if task == 'QA now':
                return {'answer': 'QA verified', 'outcome': {'status': 'completed',
                    'evidence': ['QA passed'], 'errors': [], 'remaining_work': []}}
            return {'answer': 'Patch published; QA remains', 'outcome': {'status': 'partial',
                'evidence': ['published commit'], 'errors': [], 'remaining_work': ['QA']}}
        self.runner.callback = callback
        with patch.object(FakeChat, 'get_checkpoint', return_value={'messages': []}, create=True), \
             self.service.turn(self.parent, 5):
            task = self.wait_task(self.send()['id'])
            self.assertEqual(len(calls), 1)
            self.assertEqual(task['metadata']['outcome']['status'], 'partial')
            self.assertTrue(task['metadata']['resumable'])
            self.assertEqual(task['metadata']['outcome']['evidence'], ['published commit'])
        with patch.object(FakeChat, 'get_checkpoint', return_value={'messages': []}, create=True), \
             self.service.turn(self.parent, 500):
            resumed = self.service.dispatch(self.token, 'backend_engineer', 'SendMessage', {
                'message': {'messageId': 'fresh-turn', 'taskId': task['id'], 'role': 'ROLE_USER',
                            'parts': [{'text': 'QA now'}]}})['task']
            final = self.wait_task(resumed['id'])
            self.assertEqual(final['metadata']['outcome']['status'], 'completed')
            self.assertEqual(len(self.runner.chats), 1)
            self.assertEqual(calls, ['implement', 'QA now'])

    def test_nested_coordinators_continue_partial_without_returning_to_parent(self):
        counts = {'backend_engineer': 0, 'qa_engineer': 0}
        received = []
        def callback(chat, task):
            role = chat.config['_ax_role']
            counts[role] += 1
            if counts[role] == 1:
                return {'answer': 'more work', 'outcome': {'status': 'partial',
                    'evidence': [], 'errors': [], 'remaining_work': ['finish ' + role]}}
            if role == 'backend_engineer':
                token = self.service.worker_environment(chat.session_id, chat.config)['AX_DELEGATION_TOKEN']
                received.append(self.wait_task(self.send(token=token, role='qa_engineer')['id'],
                                               token=token, role='qa_engineer'))
            return {'answer': role + ' verified', 'outcome': {'status': 'completed',
                'evidence': [role + ' diagnostic'], 'errors': [], 'remaining_work': []}}
        self.runner.callback = callback
        with patch.object(FakeChat, 'get_checkpoint', return_value={'messages': []}, create=True), \
             self.service.turn(self.parent, 5):
            result = self.wait_task(self.send()['id'])
        self.assertEqual(counts, {'backend_engineer': 2, 'qa_engineer': 2})
        self.assertEqual(result['metadata']['outcome']['status'], 'completed')
        self.assertEqual(received[0]['metadata']['outcome']['status'], 'completed')
        self.assertTrue(all(chat.closed for chat in self.runner.chats))

    def test_nested_delegation_and_cycle_rejection(self):
        nested = []
        def callback(chat, _task):
            if chat.config["_ax_role"] == "backend_engineer":
                token = self.service.worker_environment(chat.session_id, chat.config)["AX_DELEGATION_TOKEN"]
                with self.assertRaisesRegex(A2AError, "cycle"):
                    self.send(token=token, role="tech_lead")
                nested.append(self.wait_task(self.send(token=token, role="qa_engineer")["id"],
                                             token=token, role="qa_engineer"))
        self.runner.callback = callback
        with self.service.turn(self.parent, 5):
            self.wait_task(self.send()["id"])
        self.assertEqual(nested[0]["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertTrue(all(chat.closed for chat in self.runner.chats))

    def test_children_wait_in_shared_worker_queue_without_separate_capacity_rejection(self):
        runner = AxAgentRunner('localhost:5001/popot-agent-ax@sha256:' + 'a' * 64,
                               'unused', 'http://localhost:1/v1')
        from ax_local.api.worker_queue import WorkerQueue
        runner.worker_queue = WorkerQueue(10, 5)
        runner.delegation = self.service
        self.service.runners = {"test": runner}
        leases = [runner.worker_queue.acquire(f'occupied-{index}') for index in range(10)]
        def cli(*args, **_kwargs):
            return subprocess.CompletedProcess([], 1 if args[0] in {'get','describe'} else 0, '',
                                               'NotFound' if args[0] in {'get','describe'} else '')
        with patch.object(runner, '_run', side_effect=cli) as run, \
             patch.object(runner, '_wait_for_chat', side_effect=lambda chat, *_args: chat), \
             patch.object(AxChat, 'send', return_value={'answer': 'child result', 'outcome': {
                 'status': 'completed', 'evidence': ['queue fixture result'], 'remaining_work': [], 'errors': []}}):
            try:
                with self.service.turn(self.parent, 5):
                    tasks = [self.send(message_id=f'child-{index}') for index in range(4)]
                    wait_for(lambda: runner.worker_queue.status()['queued'] == 4)
                    self.assertEqual(runner.worker_queue.status()['active'], 10)
                    run.assert_not_called()
                    runner.worker_queue.release('occupied-0', leases[0])
                    for task in tasks:
                        result = self.wait_task(task['id'])
                        self.assertEqual(result['status']['state'], 'TASK_STATE_COMPLETED')
                    wait_for(lambda: not self.service.jobs)
                    self.assertEqual(runner.worker_queue.status()['active'], 9)
                    self.assertEqual(runner.worker_queue.status()['queued'], 0)
            finally:
                runner.shutdown_worker_queue()
                self.service.close()

    def test_failures_are_persisted_and_do_not_leak_exception_secrets(self):
        self.runner.callback = lambda *_: (_ for _ in ()).throw(RuntimeError("private-key"))
        with self.service.turn(self.parent, 5):
            task = self.wait_task(self.send()["id"])
        self.assertEqual(task["status"]["state"], "TASK_STATE_FAILED")
        self.assertNotIn("private-key", str(task))
        self.assertEqual(task["metadata"]["failure"], {
            "stage": "worker_message", "code": "worker_failed", "error_type": "RuntimeError"})
        self.assertTrue(self.runner.chats[0].closed)

    def test_worker_model_and_mcp_failures_have_safe_diagnostics(self):
        from popot_agents.orchestrator.main import AgentRunError
        failures = [
            ("model endpoint returned HTTP 429", "model_http_error",
             "model endpoint returned HTTP 429"),
            ("MCP server fetch failed during discovery: private-key", "mcp_discovery_failed",
             "MCP server fetch failed during discovery"),
        ]
        for index, (error, code, reason) in enumerate(failures):
            with self.subTest(code=code):
                self.runner.callback = lambda *_: (_ for _ in ()).throw(AgentRunError(error))
                with self.service.turn(self.parent, 5):
                    task = self.wait_task(self.send(message_id=f"failure-{index}")["id"])
                self.assertEqual(task["metadata"]["failure"]["code"], code)
                self.assertEqual(task["status"]["message"]["parts"][0]["text"], reason)
                self.assertNotIn("private-key", str(task))

    def test_child_startup_failure_is_distinct_from_child_model_failure(self):
        import subprocess
        with self.service.turn(self.parent, 5), \
             patch.object(self.runner, "start_chat", side_effect=subprocess.TimeoutExpired("ax", 10)):
            task = self.wait_task(self.send()["id"])
        self.assertEqual(task["metadata"]["failure"], {
            "stage": "worker_startup", "code": "ax_command_timeout", "error_type": "TimeoutExpired"})
        self.assertEqual(self.runner.chats, [])

    def test_unknown_task_and_other_parent_cannot_read_child(self):
        env = self.service.worker_environment("1111111111111111", self.roles["tech_lead"])
        with self.service.turn(self.parent, 5), self.service.turn("1111111111111111", 5):
            task = self.wait_task(self.send()["id"])
            with self.assertRaises(A2AError):
                self.service.dispatch(env["AX_DELEGATION_TOKEN"], "backend_engineer", "GetTask",
                                      {"id": task["id"]})

    def test_depth_and_total_call_limits(self):
        self.policy["max_calls_per_turn"] = 1
        with self.service.turn(self.parent, 5):
            self.wait_task(self.send()["id"])
            with self.assertRaisesRegex(A2AError, "call limit"):
                self.send(message_id="second")
        self.policy["max_depth"] = 1
        failures = []
        def callback(chat, _):
            self.assertEqual(self.service.worker_environment(chat.session_id, chat.config), {})
            # Even a caller holding a previously issued capability cannot bypass the depth limit.
            token = self.service.grants[chat.session_id].token
            with self.assertRaisesRegex(A2AError, "depth limit"):
                self.send(token=token, role="qa_engineer")
            failures.append("denied")
        self.runner.callback = callback
        self.policy["max_calls_per_turn"] = 4
        with self.service.turn(self.parent, 5):
            self.wait_task(self.send(message_id="new-turn")["id"])
        self.assertEqual(failures, ["denied"])

    def test_cancel_and_parent_completion_stop_children(self):
        release = threading.Event()
        self.runner.callback = lambda *_: release.wait(1)
        try:
            with self.service.turn(self.parent, 5):
                task = self.send()
                self.assertTrue(self.runner.entered.wait(1))
                canceled = self.service.dispatch(self.token, "backend_engineer", "CancelTask", {"id": task["id"]})
                self.assertEqual(canceled["status"]["state"], "TASK_STATE_CANCELED")
                self.assertTrue(self.runner.chats[0].closed)
                release.set()
            for job in list(self.service.jobs.values()):
                job.thread.join(1)
            self.assertEqual(self.store.get(task["id"])["roleConfig"]["_ax_delegation"]["state"],
                             "TASK_STATE_CANCELED")
        finally:
            release.set()

    def test_active_child_cannot_be_deleted_until_owner_finishes(self):
        release = threading.Event()
        self.runner.callback = lambda *_: release.wait(2)
        with patch("popot_agents.orchestrator.main.ChatServer") as server_class:
            create_server({"test": self.runner})
        handler_class = server_class.call_args.args[1]
        server = SimpleNamespace(store=self.store, live={}, live_lock=threading.RLock(),
                                 session_lock=lambda _session_id: threading.RLock(),
                                 delegation=self.service, cron=None)

        def delete(session_id):
            handler = handler_class.__new__(handler_class)
            handler.client_address = ("192.0.2.7", 1234)
            handler.command = "DELETE"
            handler.path = f"/chats/{session_id}"
            handler.request_version = "HTTP/1.1"
            handler.requestline = f"DELETE {handler.path} HTTP/1.1"
            handler.headers = Message()
            handler.server = server
            handler.wfile = io.BytesIO()
            with redirect_stderr(io.StringIO()):
                handler.do_DELETE()
            raw = handler.wfile.getvalue()
            return int(raw.split(b" ", 2)[1]), json.loads(raw.split(b"\r\n\r\n", 1)[1])

        try:
            with self.service.turn(self.parent, 5):
                session_id = self.send()["id"]
                self.assertTrue(self.runner.entered.wait(2))
                self.assertEqual(delete(session_id)[0], 409)
                self.assertIsNotNone(self.store.get(session_id))
                release.set()
                self.wait_task(session_id)
                self.assertEqual(self.store.get(session_id)["roleConfig"]["_ax_delegation"]["state"],
                                 "TASK_STATE_COMPLETED")
                self.assertEqual(delete(session_id), (200, {"status": "closed"}))
                self.assertIsNone(self.store.get(session_id))
        finally:
            release.set()

    def test_restart_marks_pending_tasks_failed_and_preserves_idempotency(self):
        with self.service.turn(self.parent, 5):
            task = self.wait_task(self.send()["id"])
        saved = self.store.get(task["id"])
        saved["roleConfig"]["_ax_delegation"]["state"] = "TASK_STATE_WORKING"
        self.store.save(saved["sessionId"], saved["agent"], saved["messages"], saved["role"], saved["roleConfig"])
        self.service.recover_interrupted()
        self.assertEqual(self.runner.recovered, [task["id"]])
        with self.service.turn(self.parent, 5):
            replay = self.send()
        self.assertEqual(replay["id"], task["id"])
        self.assertEqual(replay["status"]["state"], "TASK_STATE_FAILED")
        self.assertEqual(len(self.runner.chats), 1)

    def test_parent_turn_end_cancels_a_running_child(self):
        release = threading.Event()
        self.runner.callback = lambda *_: release.wait(1)
        try:
            with self.service.turn(self.parent, 5):
                task = self.send()
                self.assertTrue(self.runner.entered.wait(1))
            self.assertTrue(self.runner.chats[0].closed)
            release.set()
            for job in list(self.service.jobs.values()):
                job.thread.join(1)
            self.assertEqual(self.store.get(task["id"])["roleConfig"]["_ax_delegation"]["state"],
                             "TASK_STATE_CANCELED")
        finally:
            release.set()

    def test_child_completion_cancels_unfinished_grandchild(self):
        release = threading.Event()
        grandchild_started = threading.Event()
        nested = []
        def callback(chat, _):
            if chat.config["_ax_role"] == "backend_engineer":
                token = self.service.worker_environment(chat.session_id, chat.config)["AX_DELEGATION_TOKEN"]
                nested.append(self.send(token=token, role="qa_engineer")["id"])
                grandchild_started.wait(1)
            else:
                grandchild_started.set()
                release.wait(1)
        self.runner.callback = callback
        try:
            with self.service.turn(self.parent, 5):
                self.wait_task(self.send()["id"])
                release.set()
                for job in list(self.service.jobs.values()):
                    job.thread.join(1)
            saved = self.store.get(nested[0])
            self.assertEqual(saved["roleConfig"]["_ax_delegation"]["state"], "TASK_STATE_CANCELED")
            self.assertTrue(all(chat.closed for chat in self.runner.chats))
        finally:
            release.set()

    def test_existing_parent_sessions_gain_delegation_when_recreated(self):
        self.store.save(self.parent, "test", [], "tech_lead", {"tools": []})
        config = self.service.prepare_config(self.parent, {"tools": []})
        self.assertEqual(config["_ax_role"], "tech_lead")
        self.assertEqual(config["timeout_seconds"], 300)

    def test_catalog_uses_current_descriptions_and_excludes_ancestors(self):
        import json
        self.roles["qa_engineer"]["description"] = "Plan regression checks."
        config = {**self.roles["backend_engineer"], "_ax_delegation": {
            "root_session_id": self.parent, "lineage": ["tech_lead", "backend_engineer"]}}
        env = self.service.worker_environment("2222222222222222", config)
        self.assertEqual(json.loads(env["AX_DELEGATION_ROLES"]), ["qa_engineer"])
        self.assertEqual(json.loads(env["AX_DELEGATION_DESCRIPTIONS"]),
                         {"qa_engineer": "Plan regression checks."})
        self.assertEqual(self.service.card("qa_engineer")["description"], "Plan regression checks.")

    def test_saved_role_settings_cannot_expand_current_delegation_permissions(self):
        parent = "1111111111111111"
        stale = {**self.roles["tech_lead"], "allowed_roles": ["qa_engineer"]}
        env = self.service.worker_environment(parent, stale)
        import json
        self.assertEqual(json.loads(env["AX_DELEGATION_ROLES"]), ["backend_engineer"])
        with self.service.turn(parent, 5), self.assertRaisesRegex(A2AError, "not allowed"):
            self.send(token=env["AX_DELEGATION_TOKEN"], role="qa_engineer")

    def test_actual_worker_tool_waits_for_a2a_child_result(self):
        from ax_local.worker.delegation import make_tools
        server = start_a2a_server(self.service, "127.0.0.1", 0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        tools = make_tools({"AX_DELEGATION_URL": f"http://127.0.0.1:{server.server_port}",
                            "AX_DELEGATION_TOKEN": self.token,
                            "AX_DELEGATION_ROLES": '["backend_engineer"]',
                            "AX_DELEGATION_POLL_SECONDS": "0.01"})
        import json
        with self.service.turn(self.parent, 5):
            output = tools["delegate_task"]["call"]({"role": "backend_engineer", "task": "actual HTTP"},
                                                    timeout_seconds=2, call_id="call-1")
        result = json.loads(output)
        self.assertEqual(result["answer"], "child result: actual HTTP")
        self.assertEqual(result["state"], "TASK_STATE_COMPLETED")

    def test_model_selected_pr_comment_followup_uses_actual_a2a(self):
        import os
        from ax_local.worker.delegation import make_tools
        from popot_agents.worker.http_harness import run_http
        self.roles['code_reviewer'] = {'agent': 'test', 'instructions': 'review', 'tools': [],
                                       '_ax_role': 'code_reviewer', 'allowed_roles': []}
        self.roles['backend_engineer']['allowed_roles'].append('code_reviewer')
        parent = '1111111111111111'
        environment = self.service.worker_environment(parent, self.roles['backend_engineer'])
        server = start_a2a_server(self.service, '127.0.0.1', 0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        tools = make_tools({**environment,
                           'AX_DELEGATION_URL': f'http://127.0.0.1:{server.server_port}',
                           'AX_DELEGATION_POLL_SECONDS': '0.01'})
        pr = 'https://github.com/example/project/pull/70'
        history = [{'role': 'user', 'content': 'Review ' + pr + '; do not merge.'},
                   {'role': 'assistant', 'content': 'Old unverified findings.'},
                   {'role': 'user', 'content': 'оставь комментарии в PR'}]
        requests = []
        replies = [{'role': 'assistant', 'tool_calls': [{'id': 'review', 'type': 'function', 'function': {
            'name': 'delegate_task', 'arguments': json.dumps({'role': 'code_reviewer', 'task': 'Check the PR'})}}]},
                   {'role': 'assistant', 'content': 'Review child completed.'}]
        def respond(call, **kwargs):
            requests.append(json.loads(call.data))
            return io.BytesIO(json.dumps({'choices': [{'message': replies.pop(0)}]}).encode())
        with self.service.turn(parent, 5), \
             patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1',
                                     'HARNESS_MODEL': 'fake'}, clear=True), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=respond) as parent_model, \
             redirect_stderr(io.StringIO()):
            answer = run_http(history, self.roles['backend_engineer'], extra_tools=tools)
        self.assertEqual(parent_model.call_count, 2)
        self.assertEqual(len(self.runner.chats), 1)
        child = self.runner.chats[0]
        self.assertTrue(child.closed)
        saved = self.store.get(child.session_id)
        self.assertEqual(saved['role'], 'code_reviewer')
        self.assertEqual(saved['roleConfig']['_ax_delegation']['parent_session_id'], parent)
        self.assertIn(pr, saved['messages'][0]['content'])
        self.assertIn('do not merge', saved['messages'][0]['content'])
        self.assertEqual(json.loads(requests[-1]['messages'][-1]['content'])['sessionId'], child.session_id)
        self.assertEqual(answer, 'Review child completed.')

    def test_worker_retries_lost_submission_response_without_duplicate_actor(self):
        import json
        import httpx
        from ax_local.worker.delegation import make_tools
        server = start_a2a_server(self.service, "127.0.0.1", 0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        tools = make_tools({"AX_DELEGATION_URL": f"http://127.0.0.1:{server.server_port}",
                            "AX_DELEGATION_TOKEN": self.token,
                            "AX_DELEGATION_ROLES": '["backend_engineer"]',
                            "AX_DELEGATION_POLL_SECONDS": "0.01"})
        from ax_local.worker.delegation import BoundedHTTPTransport
        original = BoundedHTTPTransport.handle_async_request
        messages = []
        async def drop_response(transport, call):
            response = await original(transport, call)
            body = json.loads(call.content)
            if body["method"] == "SendMessage":
                messages.append(body["params"]["message"]["messageId"])
                if len(messages) == 1:
                    await response.aclose()
                    raise httpx.ReadError("lost response", request=call)
            return response
        with self.service.turn(self.parent, 5), \
             patch.object(BoundedHTTPTransport, "handle_async_request", new=drop_response):
            result = json.loads(tools["delegate_task"]["call"](
                {"role": "backend_engineer", "task": "retry"}, timeout_seconds=2, call_id="tool-call"))
        self.assertEqual(result["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(messages, [messages[0], messages[0]])
        self.assertEqual(len(self.runner.chats), 1)

    def test_agent_card_and_jsonrpc_over_http(self):
        import json
        from urllib import request
        server = start_a2a_server(self.service, "127.0.0.1", 0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/a2a/backend_engineer"
        with request.urlopen(url + "/.well-known/agent-card.json") as response:
            card = json.load(response)
        self.assertEqual(card["supportedInterfaces"][0]["protocolBinding"], "JSONRPC")
        with self.service.turn(self.parent, 5):
            body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {
                "message": {"messageId": "http-message", "role": "ROLE_USER",
                            "parts": [{"text": "HTTP task"}]}}}
            call = request.Request(url, data=json.dumps(body).encode(), headers={
                "Content-Type": "application/json", "A2A-Version": "1.0",
                "Authorization": "Bearer " + self.token})
            with request.urlopen(call) as response:
                result = json.load(response)
            self.assertEqual(result["id"], 1)
            self.wait_task(result["result"]["task"]["id"])

    def test_protocol_validation_rejects_malformed_history_length_before_starting_worker(self):
        from urllib import request
        server = start_a2a_server(self.service, "127.0.0.1", 0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        body = {"jsonrpc": "2.0", "id": "bad-history", "method": "SendMessage", "params": {
            "message": {"messageId": "bad-history", "role": "ROLE_USER",
                        "parts": [{"text": "must not run"}]},
            "configuration": {"returnImmediately": True, "historyLength": "invalid"}}}
        call = request.Request(f"http://127.0.0.1:{server.server_port}/a2a/backend_engineer",
                               data=json.dumps(body).encode(), headers={
            "Content-Type": "application/json", "A2A-Version": "1.0",
            "Authorization": "Bearer " + self.token})
        with self.service.turn(self.parent, 5), request.urlopen(call) as response:
            result = json.load(response)
            self.assertIn("error", result)
            self.assertEqual(result["error"]["code"], -32602)
            self.assertEqual(self.runner.chats, [])

    def sdk_server(self):
        server = start_a2a_server(self.service, "127.0.0.1", 0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.service.base_url = f"http://127.0.0.1:{server.server_port}"
        return self.service.base_url + "/a2a/backend_engineer"

    def test_official_sdk_discovers_card_submits_polls_and_cancels(self):
        import httpx
        from a2a.client.card_resolver import A2ACardResolver
        from a2a.client.transports.jsonrpc import JsonRpcTransport
        from a2a.types import a2a_pb2 as types
        from a2a.utils.errors import TaskNotFoundError, UnsupportedOperationError
        url = self.sdk_server()
        release = threading.Event()
        self.addCleanup(release.set)
        self.runner.callback = lambda *_: release.wait(2)

        async def check():
            async with httpx.AsyncClient(headers={"A2A-Version": "1.0",
                     "Authorization": "Bearer " + self.token}, trust_env=False) as http:
                card = await A2ACardResolver(http, url).get_agent_card()
                self.assertEqual(card.supported_interfaces[0].protocol_version, "1.0")
                self.assertEqual(card.supported_interfaces[0].url, url)
                transport = JsonRpcTransport(http, card, card.supported_interfaces[0].url)
                params = types.SendMessageRequest(message=types.Message(
                    message_id="sdk-cancel", role=types.ROLE_USER,
                    parts=[types.Part(text="wait for cancellation")]),
                    configuration=types.SendMessageConfiguration(return_immediately=True))
                task = (await transport.send_message(params)).task
                again = (await transport.send_message(params)).task
                self.assertEqual(task.id, again.id)
                with self.assertRaises(TaskNotFoundError):
                    await transport.get_task(types.GetTaskRequest(id="unknown"))
                with self.assertRaises(UnsupportedOperationError):
                    await transport.list_tasks(types.ListTasksRequest())
                canceled = await transport.cancel_task(types.CancelTaskRequest(id=task.id))
                self.assertEqual(canceled.status.state, types.TASK_STATE_CANCELED)
                polled = await transport.get_task(types.GetTaskRequest(id=task.id))
                self.assertEqual(polled.status.state, types.TASK_STATE_CANCELED)
                release.set()
        with self.service.turn(self.parent, 5):
            asyncio.run(check())
        self.assertLessEqual(len(self.runner.chats), 1)
        self.assertTrue(all(chat.closed for chat in self.runner.chats))

    def test_sdk_blocking_submission_waits_for_result_without_blocking_other_requests(self):
        import httpx
        from a2a.client.transports.jsonrpc import JsonRpcTransport
        from a2a.types import a2a_pb2 as types
        url = self.sdk_server()
        release = threading.Event()
        self.addCleanup(release.set)
        self.runner.callback = lambda *_: release.wait(2)

        async def check():
            async with httpx.AsyncClient(headers={"A2A-Version": "1.0",
                     "Authorization": "Bearer " + self.token}, trust_env=False) as http:
                client = JsonRpcTransport(http, types.AgentCard(), url)
                pending = asyncio.create_task(client.send_message(types.SendMessageRequest(
                    message=types.Message(message_id="sdk-blocking", role=types.ROLE_USER,
                                          parts=[types.Part(text="blocking result")]))))
                self.assertTrue(await asyncio.to_thread(self.runner.entered.wait, 1))
                self.assertFalse(pending.done())
                task_id = self.runner.chats[0].session_id
                polled = await asyncio.wait_for(client.get_task(
                    types.GetTaskRequest(id=task_id)), timeout=1)
                self.assertEqual(polled.status.state, types.TASK_STATE_WORKING)
                release.set()
                result = await asyncio.wait_for(pending, timeout=1)
                self.assertEqual(result.task.status.state, types.TASK_STATE_COMPLETED)
                self.assertEqual(result.task.artifacts[0].parts[0].text, "child result: blocking result")
        with self.service.turn(self.parent, 5):
            asyncio.run(check())

    def test_http_authority_version_and_request_size_are_enforced(self):
        import httpx
        url = self.sdk_server()
        body = {"jsonrpc": "2.0", "id": "guard", "method": "SendMessage", "params": {
            "message": {"messageId": "guard", "role": "ROLE_USER", "parts": [{"text": "deny"}]},
            "configuration": {"returnImmediately": True}}}
        with httpx.Client(trust_env=False) as http:
            self.assertEqual(http.post(url, json=body).status_code, 403)
            with self.service.turn(self.parent, 5):
                headers = {"A2A-Version": "1.0", "Authorization": "Bearer " + self.token}
                self.assertEqual(http.post(url.replace("backend_engineer", "qa_engineer"),
                                           json=body, headers=headers).status_code, 403)
                wrong_version = http.post(url, json=body, headers={**headers, "A2A-Version": "2.0"})
                self.assertEqual(wrong_version.json()["error"]["code"], -32009)
                with patch.dict("ax_local.api.a2a_sdk.RUNTIME", {"limits": {"request_bytes": 16}}):
                    self.assertEqual(http.post(url, content=b"x" * 17, headers=headers).status_code, 413)
                self.assertEqual(self.runner.chats, [])

    def test_sdk_cannot_read_another_parents_child_or_expose_storage_exceptions(self):
        import httpx
        url = self.sdk_server()
        other = "1111111111111111"
        other_token = self.service.worker_environment(other, self.roles["tech_lead"])["AX_DELEGATION_TOKEN"]
        with self.service.turn(self.parent, 5), self.service.turn(other, 5):
            task = self.wait_task(self.send()["id"])
            body = {"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": task["id"]}}
            with httpx.Client(trust_env=False) as http:
                headers = {"A2A-Version": "1.0", "Authorization": "Bearer " + other_token}
                self.assertEqual(http.post(url, json=body, headers=headers).json()["error"]["code"], -32001)
                with patch.object(self.store, "get", side_effect=OSError("private-db-credential")):
                    response = http.post(url, json=body, headers=headers)
                    self.assertEqual(response.json()["error"]["code"], -32603)
                    self.assertNotIn("private-db-credential", response.text)

    def test_worker_deadline_sends_sdk_cancel_and_releases_child(self):
        from ax_local.worker.delegation import make_tools
        url = self.sdk_server()
        release = threading.Event()
        self.addCleanup(release.set)
        self.runner.callback = lambda *_: release.wait(2)
        tools = make_tools({"AX_DELEGATION_URL": url.rsplit("/a2a/", 1)[0],
                           "AX_DELEGATION_TOKEN": self.token,
                           "AX_DELEGATION_ROLES": '["backend_engineer"]',
                           "AX_DELEGATION_POLL_SECONDS": "0.01"})
        with self.service.turn(self.parent, 5):
            started = time.monotonic()
            result = json.loads(tools["delegate_task"]["call"](
                {"role": "backend_engineer", "task": "timeout"},
                timeout_seconds=0.2, call_id="deadline"))
            self.assertLess(time.monotonic() - started, 1)
            self.assertIn("error", result)
            self.assertEqual(self.store.get(result["sessionId"])["roleConfig"]["_ax_delegation"]["state"],
                             "TASK_STATE_CANCELED")
            self.assertTrue(self.runner.chats[0].closed)
            release.set()

    def test_nested_workers_delegate_over_sdk_http(self):
        from ax_local.worker.delegation import make_tools
        self.sdk_server()
        results = []
        def callback(chat, _task):
            if chat.config["_ax_role"] == "backend_engineer":
                env = self.service.worker_environment(chat.session_id, chat.config)
                env["AX_DELEGATION_POLL_SECONDS"] = "0.01"
                results.append(json.loads(make_tools(env)["delegate_task"]["call"](
                    {"role": "qa_engineer", "task": "nested SDK"},
                    timeout_seconds=2, call_id="nested")))
        self.runner.callback = callback
        env = self.service.worker_environment(self.parent, self.roles["tech_lead"])
        env["AX_DELEGATION_POLL_SECONDS"] = "0.01"
        with self.service.turn(self.parent, 5):
            result = json.loads(make_tools(env)["delegate_task"]["call"](
                {"role": "backend_engineer", "task": "delegate onward"},
                timeout_seconds=3, call_id="parent"))
        self.assertEqual(result["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(results[0]["answer"], "child result: nested SDK")
        self.assertEqual(len(self.runner.chats), 2)
        self.assertTrue(all(chat.closed for chat in self.runner.chats))


if __name__ == "__main__":
    unittest.main()


class WorkerFailureMappingTests(unittest.TestCase):
    def test_unmatched_worker_detail_keeps_the_generic_code(self):
        from ax_local.api.delegation import _worker_failure
        from popot_agents.orchestrator.main import AgentRunError
        reason, payload = _worker_failure(AgentRunError('AX task lookup failed unexpectedly'), 'start')
        self.assertEqual(payload['code'], 'worker_failed')
        self.assertNotIn('unexpectedly', reason)

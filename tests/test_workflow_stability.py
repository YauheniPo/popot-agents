import json
import unittest

from popot_agents.orchestrator.workflow import WorkflowRun
from popot_agents.worker.delegation_policy import handoff_task
from tests.test_workflow import POLICY
from tests import test_agent_reliability as reliability


class WorkflowStabilityTests(unittest.TestCase):
    def test_continuation_reports_remaining_workflow_budget(self):
        run = WorkflowRun(POLICY, 'finish the task')
        run.begin()
        run.record({'answer':'more work','outcome':{'status':'partial','evidence':['checked'],
            'errors':[],'remaining_work':['publish']}}, {'messages':[]})
        continuation = run.continuation()
        self.assertIn('3 coordinator turns remain', continuation)
        self.assertRegex(continuation, r'\d+ seconds remain')

    def test_optional_prior_answer_cannot_displace_authorization(self):
        authority = 'Do not push. ' + 'x'*27500
        text = handoff_task('Review the requested revision', [
            {'role':'user','content':authority}, {'role':'assistant','content':'old claim '*2000}], {})
        self.assertIn(authority, text)
        self.assertLessEqual(len(text), 30000)

    def test_failed_tool_calls_do_not_count_as_progress(self):
        run = WorkflowRun(POLICY, 'fix')
        result = {'answer':'still blocked','outcome':{'status':'partial','evidence':[],
            'errors':['same input error'],'remaining_work':['correct input']}}
        for count in (1,2):
            run.begin()
            terminal = run.record(result, {'messages':[{'role':'tool','tool_call_id':str(i),
                'content':json.dumps({'isError':True,'error':'same input error'})} for i in range(count)]})
        self.assertEqual(terminal['outcome']['reason'], 'workflow_no_progress')

    def test_no_progress_explains_how_to_reconcile_a_child_checkpoint(self):
        run = WorkflowRun(POLICY, 'finish PR work')
        result = {'answer': 'No new result', 'outcome': {'status': 'partial',
            'evidence': ['commit was pushed'], 'errors': [],
            'remaining_work': ['verify review threads']}}
        for _ in range(2):
            run.begin()
            terminal = run.record(result, {'messages': []})
        self.assertEqual(terminal['outcome']['reason'], 'workflow_no_progress')
        self.assertIn('verify review threads', terminal['outcome']['remaining_work'])
        self.assertTrue(any('reconcile' in item.lower() and 'same session' in item.lower()
                            for item in terminal['outcome']['remaining_work']))

    def test_independent_runs_have_stable_distinct_identifiers(self):
        first = WorkflowRun(POLICY, 'one')
        second = WorkflowRun(POLICY, 'two')
        self.assertNotEqual(first.meta['run_id'], second.meta['run_id'])
        self.assertEqual(first.meta['run_id'], WorkflowRun(POLICY, '', first.meta).meta['run_id'])

    def test_old_review_cannot_accept_new_implementation_revision(self):
        helper = reliability.AgentReliabilityTests()
        children = {}
        for sid,role,revision in [('a','backend_engineer','2'),('b','code_reviewer','1'),('c','qa_engineer','1')]:
            children[sid*16]={'sessionId':sid*16,'role':role,'answer':'verified',
                'outcome':{'status':'completed','revision':revision*40,
                    'evidence':['verified current source'],'errors':[],'remaining_work':[]}}
        state={'messages':[{'role':'user','content':'Fix https://github.com/example/repo/pull/1'}],
            'outcome':{'status':'partial'},'completed_delegations':children}
        _,state,_=helper.run_sequence([helper.result()],state=state,
            role={'_workflow_coordinator':True,'tools':[]})
        self.assertEqual(state['outcome']['status'],'partial')
        self.assertIn('revision', ' '.join(state['outcome']['remaining_work']).lower())

    def test_matching_revision_is_accepted_and_stale_checkout_does_not_replace_pr_head(self):
        from popot_agents.worker.verification import acceptance_revision, verified_roles, observed_revision
        children=[{'role':r,'reportedAt':'2026-11-01T09:00:00Z','outcome':{
            'status':'completed','revision':'2'*40,'evidence':['verified']}}
            for r in ('backend_engineer','code_reviewer','qa_engineer')]
        receipts=[{'revision':'1'*40,'kind':'checks','status':'passed','observedAt':'2026-11-01T10:00:00Z'}]
        self.assertEqual(acceptance_revision(children,receipts),'2'*40)
        self.assertEqual(verified_roles(children,'2'*40),{'backend_engineer','code_reviewer','qa_engineer'})
        receipts.append({'revision':'3'*40,'revisionSource':'pr_head','status':'observed',
                         'observedAt':'2026-11-01T11:00:00Z'})
        self.assertEqual(acceptance_revision(children,receipts),'3'*40)
        self.assertEqual(verified_roles(children,'3'*40),set())
        self.assertIsNone(observed_revision({'revision':'4'*40},receipts))
        self.assertIsNone(observed_revision({},[{'revision':'4'*40,'status':'failed'}]))

    def test_run_history_is_bounded_and_keeps_terminal_reason(self):
        from popot_agents.orchestrator.workflow import run_history
        previous={'run_id':'prior','state':'blocked','turns':4,'outcome':{'reason':'workflow_turn_limit'}}
        saved={'sessionId':'a'*16,'roleConfig':{'_workflow':previous,
               '_workflow_runs':[{'run_id':str(i),'state':'completed'} for i in range(25)]}}
        history=run_history(saved,WorkflowRun(POLICY,'follow-up'))
        self.assertEqual(len(history),20)
        self.assertEqual(history[-1]['reason'],'workflow_turn_limit')
        self.assertEqual(history[-1]['run_id'],'prior')

    def test_native_handoff_uses_server_scope_without_automatic_continuations(self):
        from unittest.mock import Mock
        helper=reliability.AgentReliabilityTests()
        delegate=Mock(return_value=json.dumps({'sessionId':'a'*16,'role':'qa_engineer',
            'state':'TASK_STATE_COMPLETED','answer':'verified',
            'outcome':{'status':'completed','evidence':['verified'],'errors':[],'remaining_work':[]}}))
        helper.run_sequence([helper.delegate_call(),helper.result()],delegate=delegate,
            state={'messages':[{'role':'user','content':'old handoff wrapper'}],'outcome':{'status':'partial'}},
            run_context={'authorization':['Original task: do not push. Coordinator turn is a literal user phrase.'],
                         'assignments':['Review the existing change only.'],'can_delegate':True})
        task=delegate.call_args.args[0]['task']
        self.assertIn('do not push',task)
        self.assertIn('Coordinator turn is a literal user phrase',task)
        self.assertIn('Review the existing change only',task)
        self.assertNotIn('old handoff wrapper',task)

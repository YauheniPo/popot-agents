import json
import unittest

from popot_agents.orchestrator.workflow import WorkflowRun
from popot_agents.worker.delegation_policy import handoff_task
from tests.test_workflow import POLICY
from tests import test_agent_reliability as reliability


class WorkflowStabilityTests(unittest.TestCase):
    def test_pr_metadata_implementer_cannot_finish_without_observed_revision(self):
        helper = reliability.AgentReliabilityTests()
        answer, state, _ = helper.run_sequence([helper.result(answer='PR ready to merge')],
            state={'messages': [{'role': 'user', 'content':
                'Update the description of https://github.com/example/repo/pull/47'}],
                'outcome': {'status': 'partial'}},
            role={'name': 'backend_engineer', '_workflow_coordinator': True, 'tools': []},
            run_context={'session_id': 'a' * 16, 'verification_owner': 'b' * 16,
                'authorization': ['Update https://github.com/example/repo/pull/47 description']})
        self.assertEqual(state['outcome']['status'], 'partial')
        self.assertEqual(state['outcome']['reason'], 'implementation_revision_missing')
        self.assertIn('read', ' '.join(state['outcome']['remaining_work']).lower())
        self.assertNotIn('PR ready to merge', answer)

    def test_rejected_completion_does_not_present_the_unaccepted_readiness_claim(self):
        helper = reliability.AgentReliabilityTests()
        children = {'a' * 16: {'sessionId': 'a' * 16, 'role': 'backend_engineer',
            'outcome': {'status': 'completed', 'evidence': ['PR body updated']}}}
        answer, state, _ = helper.run_sequence([helper.result(answer='PR ready to merge')],
            state={'messages': [{'role': 'user', 'content':
                'Prepare https://github.com/example/repo/pull/47'}],
                'outcome': {'status': 'partial'}, 'completed_delegations': children},
            role={'name': 'planner', '_workflow_coordinator': True, 'tools': []},
            run_context={'authorization': ['Prepare https://github.com/example/repo/pull/47']})
        self.assertEqual(state['outcome']['status'], 'partial')
        self.assertNotIn('PR ready to merge', answer)
        self.assertIn(state['outcome']['reason'], answer)
        recovery = ' '.join(state['outcome']['remaining_work']).lower()
        self.assertIn('read-only', recovery)
        self.assertIn('do not repeat', recovery)

    def test_pr_metadata_result_reuses_an_actual_revision_read(self):
        helper = reliability.AgentReliabilityTests()
        revision = 'e' * 40
        for declared in (None, revision, 'f' * 40):
            with self.subTest(declared=declared):
                report = helper.result()
                if declared:
                    content = json.loads(report['content'])
                    report['content'] = json.dumps({**content, 'revision': declared})
                _, state, _ = helper.run_sequence([report], state={
                    'messages': [{'role': 'user', 'content':
                        'Update https://github.com/example/repo/pull/47 description'}],
                    'outcome': {'status': 'partial'},
                    'tool_evidence': [{'kind': 'revision', 'source': 'tool_result',
                        'status': 'observed', 'revision': revision, 'revisionSource': 'pr_head',
                        'repository': 'example/repo', 'pullRequest': 47}]},
                    role={'name': 'backend_engineer', '_workflow_coordinator': True, 'tools': []},
                    run_context={'session_id': 'a' * 16, 'verification_owner': 'b' * 16,
                        'authorization': ['Update https://github.com/example/repo/pull/47 description']})
                self.assertEqual(state['outcome']['status'], 'partial' if declared == 'f' * 40 else 'completed')
                if declared != 'f' * 40:
                    self.assertEqual(state['outcome']['revision'], revision)

    def test_metadata_child_recovers_with_one_head_read_and_existing_qa_is_reused(self):
        from unittest.mock import patch
        from tests.test_harness_loops import tool_call
        helper = reliability.AgentReliabilityTests()
        revision = 'e' * 40
        authority = 'Finalize https://github.com/example/repo/pull/47 without repeating writes'
        context = {'session_id': 'd' * 16, 'verification_owner': '0' * 16,
                   'authorization': [authority]}
        role = {'name': 'backend_engineer', '_workflow_coordinator': True, 'tools': ['bash']}
        _, state, _ = helper.run_sequence([helper.result()], role=role, run_context=context)
        self.assertEqual(state['outcome']['status'], 'partial')
        with patch('popot_agents.worker.http_harness.execute_tool',
                   return_value='exit_code=0\n' + revision + '\n') as execute:
            _, recovered, _ = helper.run_sequence([
                {'tool_calls': [tool_call('head-read', 'bash', {'command': 'git rev-parse HEAD'})]},
                helper.result()], state=state, role=role, run_context=context)
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(recovered['outcome']['status'], 'completed')
        self.assertEqual(recovered['outcome']['revision'], revision)
        children = {sid * 16: {'sessionId': sid * 16, 'role': name,
            'outcome': {'status': 'completed', 'revision': revision, 'evidence': ['verified'],
                        'verification_status': 'passed'}}
            for sid, name in [('a', 'backend_engineer'), ('b', 'code_reviewer'), ('c', 'qa_engineer')]}
        children['d' * 16] = {'sessionId': 'd' * 16, 'role': 'backend_engineer',
                             'reportedAt': '2026-11-08T11:03:04Z', 'outcome': recovered['outcome']}
        _, accepted, bodies = helper.run_sequence([helper.result()], state={
            'messages': [{'role': 'user', 'content': authority}], 'outcome': {'status': 'partial'},
            'completed_delegations': children},
            role={'name': 'planner', '_workflow_coordinator': True, 'tools': []},
            run_context={'authorization': [authority]})
        self.assertEqual(accepted['outcome']['status'], 'completed')
        self.assertEqual(accepted['outcome']['revision'], revision)
        self.assertEqual(len(bodies), 1)

    def test_resume_interruption_retains_safe_state_diagnostic(self):
        from popot_agents.orchestrator.main import AgentResumeError
        from tests import test_workflow as workflows
        failure = AgentResumeError('private transport detail')
        failure.diagnostic = {'task': 'popot-chat-' + 'a' * 16,
            'phase': 'Failed', 'reason': 'ActorSuspendFailed', 'operation': 'confirm_suspension',
            'probe': 'AX ssh timed out',
            'private': 'must not be persisted'}
        _, response, _ = workflows.WorkflowTests().run_workflow([failure], allow_error=True)
        diagnostic = response['outcome']['failure']['diagnostic']
        self.assertEqual(diagnostic['reason'], 'ActorSuspendFailed')
        self.assertEqual(diagnostic['operation'], 'confirm_suspension')
        self.assertEqual(diagnostic['probe'], 'AX ssh timed out')
        self.assertNotIn('private', diagnostic)
        self.assertNotIn('private transport detail', str(response))

    def test_planner_accepts_attributed_child_pr_readback_without_own_git_tools(self):
        helper = reliability.AgentReliabilityTests()
        revision = 'e' * 40
        children = {'a' * 16: {'sessionId': 'a' * 16, 'role': 'backend_engineer',
            'reportedAt': '2026-11-05T22:11:00Z',
            'outcome': {'status': 'completed', 'evidence': ['PR body updated']}}}
        for sid, role in [('b' * 16, 'code_reviewer'), ('c' * 16, 'qa_engineer')]:
            children[sid] = {'sessionId': sid, 'role': role, 'outcome': {
                'status': 'completed', 'revision': revision, 'evidence': ['verified']},
                'observations': [{'kind': 'revision', 'revision': revision, 'status': 'observed',
                    'sourceSessionId': sid, 'revisionSource': 'pr_head',
                    'repository': 'yauhenipo/popot-agents', 'pullRequest': 46,
                    'observedAt': '2026-11-05T22:19:00Z'}]}
        for invalid in (None, 'wrong_pr', 'wrong_repository', 'stale', 'wrong_session', 'failed_child', 'unobserved'):
            with self.subTest(invalid=invalid):
                data = json.loads(json.dumps(children))
                for child in list(data.values())[1:]:
                    observation = child['observations'][0]
                    if invalid == 'wrong_pr': observation['pullRequest'] = 47
                    if invalid == 'wrong_repository': observation['repository'] = 'other/repo'
                    if invalid == 'stale': observation['observedAt'] = '2026-11-05T22:10:00Z'
                    if invalid == 'wrong_session': observation['sourceSessionId'] = 'd' * 16
                    if invalid == 'failed_child': child['outcome']['status'] = 'failed'
                    if invalid == 'unobserved': child.pop('observations')
                state = {'messages': [{'role': 'user', 'content':
                    'Review https://github.com/YauheniPo/popot-agents/pull/46'}],
                    'outcome': {'status': 'partial'}, 'completed_delegations': data}
                _, state, _ = helper.run_sequence([helper.result()], state=state,
                    run_context={'authorization': [state['messages'][0]['content']]},
                    role={'name': 'planner', '_workflow_coordinator': True, 'tools': []})
                self.assertEqual(state['outcome']['status'], 'partial' if invalid else 'completed')
                if not invalid: self.assertEqual(state['outcome']['revision'], revision)

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

    def test_different_failed_checks_are_new_diagnostic_observations(self):
        run = WorkflowRun(POLICY, 'fix')
        checkpoint = {'messages': []}
        for failure in ('FAILED test_a: wrong default', 'FAILED test_b: wrong parsing'):
            checkpoint['messages'].append({'role': 'tool', 'tool_call_id': failure,
                                          'content': 'exit_code=1\n' + failure})
            run.begin()
            self.assertIsNone(run.record({'answer': failure, 'outcome': {'status': 'partial',
                'evidence': [failure], 'errors': [], 'remaining_work': ['fix failed check']}}, checkpoint))

    def test_repeated_failed_check_with_new_call_id_is_not_progress(self):
        run = WorkflowRun(POLICY, 'fix')
        checkpoint = {'messages': []}
        for call_id in ('first', 'retry'):
            checkpoint['messages'].append({'role': 'tool', 'tool_call_id': call_id,
                                          'content': 'exit_code=1\nFAILED test_a: wrong default'})
            run.begin()
            terminal = run.record({'answer': call_id, 'outcome': {'status': 'partial',
                'evidence': [call_id], 'errors': [], 'remaining_work': ['fix failed check']}}, checkpoint)
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

    def test_rephrasing_partial_without_new_receipts_is_not_progress(self):
        run = WorkflowRun(POLICY, 'fix and verify PR')
        checkpoint = {'messages': [{'role': 'tool', 'tool_call_id': 'review',
                                   'content': 'F1 requires an implementation fix'}]}
        for phrasing in ('Fix F1 and run QA', 'QA and the F1 correction remain'):
            run.begin()
            terminal = run.record({'answer': phrasing, 'outcome': {'status': 'partial',
                'evidence': [phrasing], 'errors': [], 'remaining_work': [phrasing]}}, checkpoint)
        self.assertEqual(terminal['outcome']['reason'], 'workflow_no_progress')

    def test_new_authorization_allows_reconciliation_of_unchanged_receipts(self):
        run = WorkflowRun(POLICY, 'fix and verify PR')
        checkpoint = {'messages': [{'role': 'tool', 'tool_call_id': 'review',
                                   'content': 'F1 requires an implementation fix'}]}
        result = {'answer': 'pending', 'outcome': {'status': 'partial', 'evidence': ['F1'],
                  'errors': [], 'remaining_work': ['fix F1']}}
        run.begin()
        self.assertIsNone(run.record(result, checkpoint))
        run.meta['authorization'].append('Only deliver the review; do not change code')
        run.begin()
        self.assertIsNone(run.record(result, checkpoint))

    def test_new_child_report_counts_as_progress_without_new_tool_messages(self):
        run = WorkflowRun(POLICY, 'fix and verify PR')
        checkpoint = {'messages': [{'role': 'tool', 'content': 'delegated review'}]}
        result = {'answer': 'pending', 'outcome': {'status': 'partial', 'evidence': ['F1'],
                  'errors': [], 'remaining_work': ['fix F1']}}
        run.begin()
        self.assertIsNone(run.record(result, checkpoint))
        checkpoint['completed_delegations'] = {'a' * 16: {'role': 'code_reviewer',
            'outcome': {'status': 'completed', 'verification_status': 'failed', 'evidence': ['F1']}}}
        run.begin()
        self.assertIsNone(run.record(result, checkpoint))

    def test_text_only_task_can_make_progress_without_tools(self):
        run = WorkflowRun(POLICY, 'write a proposal')
        for evidence in ('Scope drafted', 'Alternatives compared'):
            run.begin()
            self.assertIsNone(run.record({'answer': evidence, 'outcome': {'status': 'partial',
                'evidence': [evidence], 'errors': [], 'remaining_work': ['finish proposal']}}, {'messages': []}))

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

    def test_acceptance_uses_the_newest_pr_head_for_the_requested_pull_request(self):
        from popot_agents.worker.verification import acceptance_revision, requested_pull_request
        children=[{'role':'backend_engineer','reportedAt':'2026-11-01T09:00:00Z','outcome':{
            'status':'completed','revision':'2'*40,'evidence':['verified']}}]
        target=requested_pull_request([{'role':'user','content':
            'Review https://github.com/YauheniPo/popot-agents/pull/37 and fix it'}])
        self.assertEqual(target,{'repository':'yauhenipo/popot-agents','pullRequest':37})
        own={'revision':'3'*40,'revisionSource':'pr_head','status':'observed',
             'observedAt':'2026-11-01T10:00:00Z','repository':'yauhenipo/popot-agents','pullRequest':37}
        newer={'revision':'4'*40,'revisionSource':'pr_head','status':'observed',
               'observedAt':'2026-11-01T11:00:00Z','repository':'yauhenipo/popot-agents','pullRequest':37}
        other={'revision':'5'*40,'revisionSource':'pr_head','status':'observed',
               'observedAt':'2026-11-01T12:00:00Z','repository':'yauhenipo/popot-agents','pullRequest':38}
        unbound={'revision':'6'*40,'revisionSource':'pr_head','status':'observed',
                 'observedAt':'2026-11-01T13:00:00Z'}
        self.assertEqual(acceptance_revision(children,[newer,own],target),'4'*40)
        self.assertEqual(acceptance_revision(children,[own,newer],target),'4'*40)
        self.assertEqual(acceptance_revision(children,[own,other,unbound],target),'3'*40)
        self.assertEqual(acceptance_revision(children,[own],None),'3'*40)

    def test_ambiguous_pull_request_links_never_bind_acceptance(self):
        from popot_agents.worker.verification import AMBIGUOUS, acceptance_revision, requested_pull_request
        children=[{'role':'backend_engineer','reportedAt':'2026-11-01T09:00:00Z','outcome':{
            'status':'completed','revision':'2'*40,'evidence':['verified']}}]
        target=requested_pull_request([{'role':'user','content':
            'Compare https://github.com/YauheniPo/popot-agents/pull/37 with '
            'https://github.com/YauheniPo/popot-agents/pull/38'}])
        self.assertIs(target,AMBIGUOUS)
        foreign={'revision':'5'*40,'revisionSource':'pr_head','status':'observed',
                 'observedAt':'2026-11-01T12:00:00Z','repository':'other/repo','pullRequest':9}
        unbound={'revision':'6'*40,'revisionSource':'pr_head','status':'observed',
                 'observedAt':'2026-11-01T13:00:00Z'}
        self.assertEqual(acceptance_revision(children,[foreign,unbound],target),'2'*40)
        self.assertEqual(acceptance_revision(children,[unbound]),'6'*40)

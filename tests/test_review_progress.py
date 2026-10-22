"""Reviewer checkpoints must stop tool execution once no work remains."""
import json
import unittest
from unittest.mock import Mock

from tests import test_agent_reliability as helpers


def call(name, args):
    return {'id': name, 'function': {'name': name, 'arguments': json.dumps(args)}}


def checkpoint(pending=None):
    return call('review_checkpoint', {'covered_files': ['app.py'],
        'decisions': ['Rejected timeout candidate: test passes on base and head.'],
        'remaining_work': pending or []})


class ReviewProgressTests(unittest.TestCase):
    run_sequence = helpers.AgentReliabilityTests.run_sequence
    result = staticmethod(helpers.AgentReliabilityTests.result)
    role = {'name': 'code_reviewer', 'tools': ['calculate']}

    def test_checkpoint_deltas_preserve_coverage_and_decision_history(self):
        from popot_agents.worker.review_progress import ReviewProgress
        progress = ReviewProgress()
        first = {'covered_files': ['a.py'], 'decisions': ['Rejected A: test passed'],
                 'remaining_work': ['Inspect b.py']}
        progress.record(first, timeout_seconds=10, call_id='first')
        output = json.loads(progress.record({'covered_files': [' b.py ', 'a.py'],
            'decisions': ['Rejected A: test passed', 'Supersedes A: new failing test'],
            'remaining_work': []}, timeout_seconds=10, call_id='second'))
        self.assertEqual(output['reported_progress']['covered_files'], ['a.py', 'b.py'])
        self.assertEqual(output['reported_progress']['decisions'],
                         ['Rejected A: test passed', 'Supersedes A: new failing test'])
        self.assertEqual(output['reported_progress']['remaining_work'], [])
        self.assertEqual(first['covered_files'], ['a.py'])

    def test_next_work_request_focuses_on_latest_remaining_work(self):
        _, _, bodies = self.run_sequence([
            {'tool_calls': [checkpoint(['Inspect only the remaining parser branch'])]},
            {'tool_calls': [call('calculate', {'expression': '2+2'})]}, self.result()], role=self.role)
        for body in bodies[1:]:
            focus = body['messages'][-1]
            self.assertEqual(focus['role'], 'system')
            self.assertIn('Inspect only the remaining parser branch', focus['content'])
            self.assertIn('new evidence', focus['content'])
            self.assertIn('Rejected timeout candidate', focus['content'])

    def test_checkpoint_with_no_remaining_work_forces_tool_free_final(self):
        _, state, bodies = self.run_sequence([
            {'tool_calls': [checkpoint()]}, self.result()], role=self.role)
        self.assertEqual(bodies[-1].get('tool_choice'), 'none')
        self.assertNotIn('tools', bodies[-1])
        self.assertEqual(state['review_progress']['covered_files'], ['app.py'])
        self.assertEqual(state['outcome']['status'], 'completed')

    def test_checkpoint_final_partial_does_not_claim_budget_exhaustion(self):
        answer, state, _ = self.run_sequence([
            {'tool_calls': [checkpoint()]}, self.result('partial', 'CI remains unverified.',
                remaining=['Verify CI'])], role=self.role)
        self.assertEqual(state['outcome']['status'], 'partial')
        self.assertEqual(state['outcome']['reason'], 'agent_reported')
        self.assertNotIn('budget exhausted', answer)

    def test_pending_work_keeps_tools_available_and_decisions_in_history(self):
        _, state, bodies = self.run_sequence([
            {'tool_calls': [checkpoint(['Verify the publication response'])]}, self.result()], role=self.role)
        self.assertIn('tools', bodies[-1])
        self.assertIn('Rejected timeout candidate', json.dumps(bodies[-1]['messages']))
        self.assertEqual(state['review_progress']['remaining_work'], ['Verify the publication response'])

    def test_six_work_rounds_require_checkpoint_before_more_work(self):
        calc = {'tool_calls': [call('calculate', {'expression': '2+2'})]}
        _, _, bodies = self.run_sequence([*[calc]*6, {'tool_calls': [checkpoint()]}, self.result()], role=self.role)
        self.assertEqual([s['function']['name'] for s in bodies[6]['tools']], ['review_checkpoint'])
        self.assertNotIn('tools', bodies[7])

    def test_same_batch_cannot_delegate_after_reporting_no_remaining_work(self):
        delegate = Mock(return_value='must not execute')
        _, _, bodies = self.run_sequence([{'tool_calls': [checkpoint(),
            call('delegate_task', {'role': 'qa_engineer', 'task': 'Look harder'})]}, self.result()],
            role=self.role, delegate=delegate)
        delegate.assert_not_called()
        self.assertNotIn('tools', bodies[-1])

    def test_checkpoint_only_round_rejects_work_even_if_checkpoint_reopens_tools(self):
        delegate = Mock(return_value='must not execute')
        calc = {'tool_calls': [call('calculate', {'expression': '2+2'})]}
        self.run_sequence([*[calc]*6, {'tool_calls': [checkpoint(['Run required test']),
            call('delegate_task', {'role': 'qa_engineer', 'task': 'Run test'})]}, self.result()],
            role=self.role, delegate=delegate)
        delegate.assert_not_called()

    def test_invalid_checkpoint_does_not_mark_review_finished(self):
        _, state, bodies = self.run_sequence([{'tool_calls': [call('review_checkpoint',
            {'covered_files': [], 'decisions': [], 'remaining_work': 'none'})]}, self.result()], role=self.role)
        self.assertIn('tools', bodies[-1])
        self.assertNotIn('review_progress', state)

    def test_nonreview_role_has_no_checkpoint_tool(self):
        _, _, bodies = self.run_sequence([self.result()], role={'tools': ['calculate']})
        self.assertNotIn('review_checkpoint', json.dumps(bodies[0]['tools']))

    def test_partial_resume_preserves_decisions_but_allows_new_work(self):
        _, state, _ = self.run_sequence([
            {'tool_calls': [checkpoint(['Run integration tests'])]}, self.result('partial',
            remaining=['Run integration tests'])], role=self.role)
        _, state, bodies = self.run_sequence([self.result()], role=self.role, state=state)
        self.assertIn('tools', bodies[0])
        self.assertIn('Rejected timeout candidate', json.dumps(bodies[0]['messages']))
        self.assertEqual(state['outcome']['status'], 'completed')

    def test_invalid_checkpoint_keeps_required_checkpoint_round_active(self):
        calc = {'tool_calls': [call('calculate', {'expression': '2+2'})]}
        _, _, bodies = self.run_sequence([*[calc]*6,
            {'tool_calls': [call('review_checkpoint', {})]}, {'tool_calls': [checkpoint()]},
            self.result()], role=self.role)
        for body in bodies[6:8]:
            self.assertEqual([s['function']['name'] for s in body['tools']], ['review_checkpoint'])
        self.assertNotIn('tools', bodies[-1])

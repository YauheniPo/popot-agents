import json
import unittest
from unittest.mock import Mock
from unittest.mock import patch

from tests import test_agent_reliability as reliability


class ChildRecoveryTests(unittest.TestCase):
    run_sequence = reliability.AgentReliabilityTests.run_sequence
    result = staticmethod(reliability.AgentReliabilityTests.result)

    def test_malformed_child_check_claims_cannot_unlock_recovery(self):
        from popot_agents.child_results import budget_recovery_allowed
        receipt = {'kind': 'checks', 'source': 'tool_result', 'status': 'passed', 'revision': 'b' * 40,
            'commandSha256': 'd' * 64, 'outputSha256': 'e' * 64, 'summary': '3 passed', 'exitCode': 0}
        malformed = [[{**receipt, field: value}] for field, value in (
            ('status', []), ('summary', []), ('revision', {}), ('outputSha256', 'invalid'), ('exitCode', '0'))]
        for checks in (None, '3 passed', {}, [None], *malformed):
            with self.subTest(checks=checks):
                failure = self.checkpoint()['failed_delegations'][0][1]
                failure['outcome']['checks'] = checks
                self.assertFalse(budget_recovery_allowed(failure, 1, 2))

    def test_role_can_keep_a_smaller_recovery_limit(self):
        state = self.checkpoint()
        state['child_recoveries'] = {'a' * 16: 1}
        delegate = Mock(return_value=self.completed())
        _, state, _ = self.run_sequence([
            self.continuation(), self.result('partial', remaining=['verify'])],
            delegate=delegate, state=state,
            role={'name': 'planner', 'tools': [], 'coordination': {'max_child_recoveries': 1}})
        delegate.assert_not_called()
        self.assertEqual(state['outcome']['status'], 'blocked')

    def test_second_budget_recovery_requires_new_tool_observed_checks(self):
        from popot_agents.runtime_config import RUNTIME
        for progress in (False, True):
            with self.subTest(progress=progress):
                checkpoint = self.checkpoint()
                checkpoint['child_recoveries'] = {'a' * 16: 1}
                failure = checkpoint['failed_delegations'][0][1]
                receipt = {'kind': 'checks', 'source': 'tool_result', 'status': 'passed',
                    'revision': 'b' * 40, 'commandSha256': 'd' * 64,
                    'outputSha256': 'e' * 64, 'summary': '3 passed', 'exitCode': 0}
                failure['previous_failure'] = {'role': 'qa_engineer', 'session_id': 'a' * 16,
                    'outcome': {**failure['outcome'], 'checks': [receipt]}}
                failure['outcome']['checks'] = [{**receipt, 'callId': 'repeated-command',
                    'outputSha256': 'f' * 64, 'summary': '4 passed' if progress else '3 passed'}]
                delegate = Mock(return_value=self.completed())
                with patch.dict(RUNTIME['workflow'], max_child_recoveries=2):
                    _, state, _ = self.run_sequence([
                        self.continuation('b' * 40), self.result('partial', remaining=['verify'])],
                        delegate=delegate, state=checkpoint)
                self.assertEqual(delegate.call_count, int(progress))
                self.assertEqual(state['child_recoveries']['a' * 16], 2 if progress else 1)
                self.assertEqual(bool(state.get('resolved_delegations')), progress)

    def test_exhausted_configured_recovery_budget_still_blocks(self):
        from popot_agents.runtime_config import RUNTIME
        state = self.checkpoint()
        state['child_recoveries'] = {'a' * 16: 2}
        delegate = Mock(return_value=self.completed())
        with patch.dict(RUNTIME['workflow'], max_child_recoveries=2):
            _, state, _ = self.run_sequence([
                self.continuation(), self.result('partial', remaining=['verify'])],
                delegate=delegate, state=state)
        delegate.assert_not_called()
        self.assertEqual(state['outcome']['status'], 'blocked')

    def checkpoint(self, reason='workflow_turn_limit', role='qa_engineer'):
        return {'messages': [{'role': 'user', 'content': 'Verify the existing artifact'}],
                'outcome': {'status': 'partial'},
                'failed_delegations': [[[role, 'verify'], {
                    'role': role, 'session_id': 'a' * 16, 'status': 'blocked',
                    'resumable': True, 'pending': False, 'error': 'Child incomplete',
                    'outcome': {'status': 'blocked', 'reason': reason,
                                'evidence': ['artifact already written'],
                                'remaining_work': ['Read back the artifact'], 'errors': []}}]]}

    def continuation(self, revision=None):
        arguments = {'role': 'qa_engineer', 'session_id': 'a' * 16,
                     'task': 'Read back the artifact; preserve existing writes'}
        if revision:
            arguments['revision'] = revision
        return {'tool_calls': [{'id': 'resume', 'function': {
            'name': 'delegate_task', 'arguments': json.dumps(arguments)}}]}

    def completed(self, revision='b' * 40, evidence=None):
        return json.dumps({'sessionId': 'a' * 16, 'role': 'qa_engineer',
                           'state': 'TASK_STATE_COMPLETED', 'answer': 'Artifact verified',
                           'outcome': {'status': 'completed', 'revision': revision,
                                       'evidence': ['read-back verified'] if evidence is None else evidence,
                                       'remaining_work': [], 'errors': []}})

    def test_budget_blocker_returns_recoverable_partial(self):
        _, state, _ = self.run_sequence([self.result('partial', remaining=['Read back the artifact'])],
                                         state=self.checkpoint())
        self.assertEqual(state['outcome']['status'], 'partial')
        self.assertEqual(state['outcome']['reason'], 'child_recovery_required')
        self.assertIn('a' * 16, ' '.join(state['outcome']['remaining_work']))

    def test_user_blocker_and_spent_recovery_remain_blocked(self):
        for reason, spent in [('permission_required', False), ('workflow_no_progress', False),
                              ('workflow_turn_limit', True)]:
            with self.subTest(reason=reason, spent=spent):
                state = self.checkpoint(reason)
                if spent:
                    state['child_recoveries'] = {'a' * 16: 1}
                _, state, _ = self.run_sequence([self.result('partial', remaining=['Read back'])], state=state)
                self.assertEqual(state['outcome']['status'], 'blocked')

    def test_successful_same_session_recovery_records_resolution(self):
        delegate = Mock(return_value=self.completed())
        _, state, _ = self.run_sequence([self.continuation('b' * 40), self.result()],
                                         delegate=delegate, state=self.checkpoint())
        delegate.assert_called_once()
        self.assertEqual(state['failed_delegations'], [])
        self.assertEqual(state['child_recoveries'], {'a' * 16: 1})
        resolution = state['resolved_delegations'][0]
        self.assertEqual(resolution['session_id'], 'a' * 16)
        self.assertEqual(resolution['previous_reason'], 'workflow_turn_limit')
        self.assertEqual(resolution['revision'], 'b' * 40)
        self.assertEqual(resolution['evidence'], ['read-back verified'])

    def test_repeated_budget_failure_is_not_submitted_twice(self):
        delegate = Mock(return_value=json.dumps({'sessionId': 'a' * 16, 'role': 'qa_engineer',
            'state': 'TASK_STATE_COMPLETED', 'resumable': True, 'answer': 'Read-back still pending',
            'outcome': {'status': 'blocked', 'reason': 'workflow_turn_limit',
                        'evidence': ['artifact exists'], 'remaining_work': ['Read back'], 'errors': []}}))
        _, state, _ = self.run_sequence([self.continuation(), self.result('partial', remaining=['Read back'])],
                                         delegate=delegate, state=self.checkpoint())
        self.assertEqual(state['outcome']['status'], 'blocked')
        _, state, _ = self.run_sequence([self.continuation(), self.result('partial', remaining=['Read back'])],
                                         delegate=delegate, state=state)
        self.assertEqual(delegate.call_count, 1)
        self.assertEqual(state['child_recoveries'], {'a' * 16: 1})

    def test_recovery_crosses_checkpoint_without_losing_prior_evidence(self):
        _, state, _ = self.run_sequence([self.result('partial', remaining=['Read back'])], state=self.checkpoint())
        delegate = Mock(return_value=self.completed())
        _, state, _ = self.run_sequence([self.continuation('b' * 40), self.result()], delegate=delegate, state=state)
        self.assertEqual(state['outcome']['status'], 'completed')
        self.assertEqual(len(state['resolved_delegations']), 1)


    def test_stale_revision_or_empty_evidence_cannot_resolve_failure(self):
        for revision, evidence in [('c' * 40, ['old revision checked']), ('b' * 40, [])]:
            with self.subTest(revision=revision, evidence=evidence):
                delegate = Mock(return_value=self.completed(revision, evidence))
                _, state, _ = self.run_sequence([
                    self.continuation('b' * 40), self.result('partial', remaining=['Verify current artifact'])],
                    delegate=delegate, state=self.checkpoint())
                self.assertTrue(state['failed_delegations'])
                self.assertEqual(state['failed_delegations'][0][1]['status'], 'failed')
                self.assertFalse(state.get('resolved_delegations'))

    def test_server_target_revision_is_required_even_without_model_revision_argument(self):
        delegate = Mock(return_value=self.completed('c' * 40))
        _, state, _ = self.run_sequence([self.continuation(), self.result('partial', remaining=['Verify current SHA'])],
            delegate=delegate, state=self.checkpoint(), run_context={'target_revision': 'b' * 40})
        self.assertTrue(state['failed_delegations'])
        self.assertFalse(state.get('resolved_delegations'))

    def test_successful_qa_does_not_clear_backend_failure(self):
        from popot_agents.work_evidence import child_resolution
        evidence = json.loads(self.completed())
        with self.assertRaisesRegex(RuntimeError, 'unresolved work'):
            child_resolution({'role': 'backend_engineer'}, evidence)
        delegate = Mock(return_value=self.completed())
        new_qa = self.continuation()
        arguments = json.loads(new_qa['tool_calls'][0]['function']['arguments'])
        arguments.pop('session_id')
        new_qa['tool_calls'][0]['function']['arguments'] = json.dumps(arguments)
        _, state, _ = self.run_sequence([new_qa, self.result('partial', remaining=['Read back'])],
                                         delegate=delegate, state=self.checkpoint(role='backend_engineer'))
        self.assertEqual(state['failed_delegations'][0][1]['role'], 'backend_engineer')

    def test_child_does_not_start_duplicate_verification_owned_by_parent(self):
        delegate = Mock(return_value=self.completed())
        _, _, bodies = self.run_sequence([self.continuation(), self.result()], delegate=delegate,
            role={'name': 'backend_engineer', 'tools': []},
            run_context={'verification_owner': 'b' * 16, 'session_id': 'c' * 16})
        delegate.assert_not_called()
        instructions = ' '.join(m['content'] for m in bodies[0]['messages'] if m['role'] == 'system')
        self.assertIn('verification owner', instructions.lower())


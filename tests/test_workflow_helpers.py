"""Branch coverage for the workflow outcome, receipt and continuation helpers."""
import json
import unittest
from types import SimpleNamespace

from popot_agents.orchestrator.workflow import (
    _outcome_rejection,
    _pending_child_reports,
    _progress_material,
    _tool_message_receipt,
    _turn_prefix,
)


class OutcomeRejectionTests(unittest.TestCase):
    """An outcome is recordable only with the documented evidence contract."""

    def test_missing_outcome_is_rejected(self):
        self.assertEqual(_outcome_rejection(None), 'workflow_missing_outcome')

    def test_non_string_evidence_is_invalid(self):
        outcome = {'status': 'partial', 'evidence': 'checked',
                   'remaining_work': [], 'errors': []}
        self.assertEqual(_outcome_rejection(outcome), 'workflow_invalid_outcome')

    def test_partial_outcome_with_evidence_is_recordable(self):
        outcome = {'status': 'partial', 'evidence': ['checked'],
                   'remaining_work': ['publish'], 'errors': []}
        self.assertIsNone(_outcome_rejection(outcome))

    def test_completed_outcome_needs_evidence(self):
        outcome = {'status': 'completed', 'evidence': [' '],
                   'remaining_work': [], 'errors': []}
        self.assertEqual(_outcome_rejection(outcome), 'workflow_unverified_completion')

    def test_completed_outcome_rejects_unfinished_work(self):
        outcome = {'status': 'completed', 'evidence': ['checked'],
                   'remaining_work': ['publish'], 'errors': []}
        self.assertEqual(_outcome_rejection(outcome), 'workflow_unverified_completion')

    def test_completed_outcome_with_evidence_is_recordable(self):
        outcome = {'status': 'completed', 'evidence': ['checked'],
                   'remaining_work': [], 'errors': []}
        self.assertIsNone(_outcome_rejection(outcome))


class ToolMessageReceiptTests(unittest.TestCase):
    """Tool messages count as work only when they carry an observed result."""

    def test_pending_message_proves_no_work(self):
        item = {'role': 'tool', 'content': json.dumps({'pending': True})}
        self.assertIsNone(_tool_message_receipt(item))

    def test_failure_digest_distinguishes_a_failed_command(self):
        kind, digest = _tool_message_receipt({'content': 'exit_code=1\nboom\n'})
        self.assertEqual(kind, 'failure')
        self.assertEqual(len(digest), 64)

    def test_successful_command_is_a_receipt(self):
        kind, _ = _tool_message_receipt({'tool_call_id': 'c1', 'content': 'exit_code=0\nok\n'})
        self.assertEqual(kind, 'receipt')

    def test_plain_message_is_a_receipt(self):
        kind, _ = _tool_message_receipt({'tool_call_id': 'c2', 'content': 'plain output'})
        self.assertEqual(kind, 'receipt')


class ProgressMaterialTests(unittest.TestCase):
    """A rewritten summary cannot replace observed tool work."""

    def test_pending_tool_messages_are_skipped(self):
        checkpoint = {'messages': [
            {'role': 'tool', 'content': json.dumps({'pending': True})},
            {'role': 'assistant', 'content': 'summary'},
        ]}
        outcome = {'evidence': ['checked'], 'remaining_work': [], 'errors': []}
        material = _progress_material(checkpoint, outcome, 'authorized task')
        self.assertEqual(material['tool_receipts'], [])
        self.assertEqual(material['authorization'], 'authorized task')

    def test_completed_delegations_replace_text_material(self):
        checkpoint = {'completed_delegations': {'s1': {'role': 'backend_engineer'}}}
        outcome = {'evidence': ['checked'], 'remaining_work': [], 'errors': []}
        material = _progress_material(checkpoint, outcome, 'authorized task')
        self.assertEqual(material, {'completed_delegations': {'s1': {'role': 'backend_engineer'}},
                                    'tool_receipts': [], 'failed_observations': [],
                                    'authorization': 'authorized task'})


class PendingChildTests(unittest.TestCase):
    """Only a pending child report keeps the workflow run alive."""

    def test_pending_child_report(self):
        checkpoint = {'failed_delegations': [['backend_engineer', {'pending': True}]]}
        self.assertTrue(_pending_child_reports(checkpoint))

    def test_checkpoint_without_delegations(self):
        self.assertFalse(_pending_child_reports({}))


class TurnPrefixTests(unittest.TestCase):
    """The continuation prompt names the turn it resumes."""

    def test_next_turn_prefix(self):
        workflow = SimpleNamespace(meta={'turns': 2})
        self.assertIn('begin turn 3', _turn_prefix(workflow))

    def test_verification_prefix(self):
        workflow = SimpleNamespace(meta={'turns': 3, 'awaiting_children': True,
                                         'verificationPhaseStarted': True})
        self.assertIn('for verification', _turn_prefix(workflow))

    def test_child_wait_prefix(self):
        workflow = SimpleNamespace(meta={'turns': 4, 'awaiting_children': True})
        self.assertIn('Child wait finished', _turn_prefix(workflow))

"""Branch coverage for verification outcomes, receipts and publication guards."""
import json
import unittest

from popot_agents.tools import _artifact_failure
from popot_agents.worker.delegation_policy import _publication_command
from popot_agents.worker.final_result import _observation_receipt
from popot_agents.worker.verification import (
    unresolved_findings,
    validate_findings,
    verification_failed,
)
from popot_agents.work_evidence import evidence_event


class VerificationFindingsTests(unittest.TestCase):
    """Findings validation and open in-scope detection."""

    def test_findings_must_be_a_list(self):
        with self.assertRaises(ValueError):
            validate_findings('not a list')

    def test_missing_outcome_has_no_unresolved_findings(self):
        self.assertEqual(unresolved_findings(None), [])

    def test_non_list_findings_are_ignored(self):
        self.assertEqual(unresolved_findings({'findings': 'not a list'}), [])

    def test_invalid_findings_fail_verification(self):
        self.assertTrue(verification_failed({'findings': 'not a list'}))
        self.assertTrue(verification_failed(None))

    def test_passed_outcome_without_open_findings(self):
        self.assertFalse(verification_failed({'findings': [], 'verification_status': 'passed'}))


class ArtifactFailureTests(unittest.TestCase):
    """Artifact diagnostics survive only the documented envelope."""

    def test_non_json_output_has_no_diagnostic(self):
        self.assertIsNone(_artifact_failure(b'not json'))

    def test_non_mapping_output_has_no_diagnostic(self):
        self.assertIsNone(_artifact_failure(b'[]'))


class PublicationCommandTests(unittest.TestCase):
    """Publication detection reads git/gh writes and rejects unknown options."""

    def test_git_global_option_before_commit(self):
        self.assertEqual(_publication_command(['git', '-c', 'user.name=x', 'commit']), 'commit')

    def test_unknown_git_option_is_not_a_publication(self):
        self.assertIsNone(_publication_command(['git', '--unknown-option', 'commit']))

    def test_gh_repo_option_before_pr_create(self):
        self.assertEqual(_publication_command(['gh', '-Racme/repo', 'pr', 'create']), 'push')

    def test_non_publication_program(self):
        self.assertIsNone(_publication_command(['ls', '-la']))


class ObservationReceiptTests(unittest.TestCase):
    """Check receipts keep exit codes, skips and full-output pointers."""

    def test_receipt_carries_skip_and_output_details(self):
        receipt = _observation_receipt({
            'kind': 'checks', 'tool': 'bash', 'callId': 'call-1', 'status': 'failed',
            'summary': 'pytest', 'revision': None, 'exitCode': 1, 'skipCount': 2,
            'unknownSkipReasons': 1, 'outputSha256': 'abc',
            'outputArtifact': '.tool-results/out.txt', 'skippedTests': [{'name': 'a'}]}, 4000)
        self.assertIn('exit_code=1', receipt)
        self.assertIn('skipped=2', receipt)
        self.assertIn('Full output: .tool-results/out.txt', receipt)
        self.assertIn('skip details', receipt)


class EvidenceEventTests(unittest.TestCase):
    """The evidence event accepts only the bounded receipt contract."""

    def test_non_json_message_is_ignored(self):
        self.assertIsNone(evidence_event('not json'))

    def test_unknown_kind_is_ignored(self):
        message = json.dumps({'event': 'work_evidence', 'receipt': {
            'source': 'tool_result', 'kind': 'unknown', 'status': 'observed'}})
        self.assertIsNone(evidence_event(message))

    def test_receipt_without_required_fields_is_ignored(self):
        message = json.dumps({'event': 'work_evidence', 'receipt': {
            'source': 'tool_result', 'kind': 'checks', 'status': 'observed',
            'callId': 'call-1', 'tool': 'bash', 'observedAt': '2026-01-01T00:00:00Z'}})
        self.assertIsNone(evidence_event(message))

    def test_valid_receipt_is_accepted(self):
        message = json.dumps({'event': 'work_evidence', 'receipt': {
            'source': 'tool_result', 'kind': 'checks', 'status': 'observed',
            'callId': 'call-1', 'tool': 'bash', 'observedAt': '2026-01-01T00:00:00Z',
            'summary': 'pytest passed'}})
        accepted = evidence_event(message)
        self.assertIsNotNone(accepted)
        self.assertEqual(accepted['callId'], 'call-1')

"""Exercise revision checkout against real Git objects, without network access."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from popot_agents import tools


class GitRevisionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.com')
        (self.source / 'version.txt').write_text('main')
        self.git('add', '.')
        self.git('commit', '-m', 'Main fixture')
        self.git('checkout', '-b', 'review')
        (self.source / 'version.txt').write_text('review')
        self.git('commit', '-am', 'Review fixture')
        self.sha = self.git('rev-parse', 'HEAD').strip()
        self.git('checkout', 'main')
        # Keep the real command runner; only replace the public remote with a fixture.
        for patcher in (patch.object(tools, 'WORKSPACE_ROOT', self.root),
                        patch.object(tools, '_public_url', return_value=self.source.as_uri())):
            patcher.start()
            self.addCleanup(patcher.stop)

    def git(self, *arguments):
        return subprocess.check_output(['git', '-C', str(self.source), *arguments],
                                       stderr=subprocess.STDOUT, text=True)

    def clone(self, sha):
        return tools.execute_tool('git_clone', {'url': 'https://example.com/repo.git',
            'directory': 'checkout', 'commit_sha': sha}, ['git_clone'])

    def test_nondefault_commit_is_checked_out_and_verified(self):
        result = self.clone(self.sha)
        self.assertEqual((self.root / 'checkout/version.txt').read_text(), 'review')
        head = subprocess.check_output(['git', '-C', str(self.root / 'checkout'),
                                        'rev-parse', 'HEAD'], text=True).strip()
        self.assertEqual(head, self.sha)
        self.assertIn('verified_commit=' + self.sha, result)

    def test_verified_clone_revision_reaches_coordinator_acceptance_after_report_correction(self):
        from tests.test_agent_reliability import AgentReliabilityTests
        helper = AgentReliabilityTests()
        clone = {'tool_calls': [{'id': 'clone', 'function': {'name': 'git_clone',
            'arguments': json.dumps({'url': 'https://example.com/repo.git',
                'directory': 'checkout', 'commit_sha': self.sha.upper()})}}]}
        _, child, _ = helper.run_sequence([clone, helper.result()],
            role={'name': 'backend_engineer', 'tools': ['git_clone']})
        children = {'a' * 16: {'sessionId': 'a' * 16, 'role': 'backend_engineer',
            'reportedAt': '2026-11-06T21:09:10Z', 'outcome': child['outcome']}}
        # Reviewer/QA inspected the same immutable source before the report was
        # reissued; the later producer only corrected report citations.
        for sid, role in [('b' * 16, 'code_reviewer'), ('c' * 16, 'qa_engineer')]:
            children[sid] = {'sessionId': sid, 'role': role, 'outcome': {
                'status': 'completed', 'revision': self.sha, 'evidence': ['source verified']},
                'observations': [{'kind': 'revision', 'revision': self.sha, 'status': 'observed',
                    'sourceSessionId': sid, 'revisionSource': 'pr_head',
                    'repository': 'example/repo', 'pullRequest': 47,
                    'observedAt': '2026-11-06T21:03:59Z'}]}
        state = {'messages': [{'role': 'user', 'content':
            'Diagnose https://github.com/example/repo/pull/47 without source changes'}],
            'outcome': {'status': 'partial'}, 'completed_delegations': children}
        _, state, _ = helper.run_sequence([helper.result()], state=state,
            role={'name': 'tech_lead', '_workflow_coordinator': True, 'tools': []},
            run_context={'authorization': [state['messages'][0]['content']]})
        self.assertEqual(state['outcome']['status'], 'completed', state['outcome'])
        self.assertEqual(child['outcome']['revision'], self.sha)
        self.assertEqual(state['outcome']['revision'], self.sha)
        receipt = child['tool_evidence'][0]
        self.assertEqual(receipt['tool'], 'git_clone')
        self.assertNotIn('revisionSource', receipt)

    def test_missing_commit_does_not_leave_default_branch_source(self):
        with self.assertRaises(tools.ToolExecutionError):
            self.clone('a' * 40)
        self.assertFalse((self.root / 'checkout/version.txt').exists())

    def test_invalid_sha_is_rejected_before_any_command(self):
        for sha in ('main', 'abc123', '--upload-pack=bad', None, 123):
            with self.subTest(sha=sha), patch.object(tools, '_run_command') as command:
                with self.assertRaisesRegex(tools.ToolInputError, 'commit_sha'):
                    self.clone(sha)
                command.assert_not_called()

    def test_failed_checkout_never_reports_verified_success(self):
        with patch.object(tools, '_run_command', side_effect=[
                'exit_code=0\n', 'exit_code=0\n', 'exit_code=1\ncheckout failed']) as command:
            with self.assertRaises(tools.ToolExecutionError):
                self.clone(self.sha)
            self.assertEqual(command.call_count, 3)

    def test_head_mismatch_never_reports_verified_success(self):
        with patch.object(tools, '_run_command', side_effect=[
                'exit_code=0\n'] * 3 + ['exit_code=0\n' + 'b' * 40]):
            with self.assertRaisesRegex(tools.ToolExecutionError, 'HEAD does not match'):
                self.clone(self.sha)

    def test_process_timeout_returns_a_recoverable_unverified_checkout_error(self):
        with patch.object(tools, '_run_command', side_effect=RuntimeError('command timed out')):
            with self.assertRaisesRegex(tools.ToolExecutionError, 'unverified'):
                self.clone(self.sha)

    def test_clone_steps_share_one_deadline(self):
        with patch.object(tools.time, 'monotonic', side_effect=[0, 1, 10]), \
                patch.object(tools, '_run_command', return_value='exit_code=0\n') as command:
            with self.assertRaisesRegex(tools.ToolExecutionError, 'timed out'):
                tools.execute_tool('git_clone', {'url': 'https://example.com/repo.git',
                    'directory': 'checkout', 'commit_sha': self.sha}, ['git_clone'], timeout_seconds=10)
            command.assert_called_once()
            self.assertEqual(command.call_args.kwargs['timeout'], 9)

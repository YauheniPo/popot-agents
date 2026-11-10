"""Repository guidance discovery and turn-budget validation, exercised in process."""
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from popot_agents import tools
from popot_agents.tools import MAX_TOOL_OUTPUT
from popot_agents.worker import file_worker, http_harness


class RepositoryContextTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        patcher = patch.object(file_worker, 'WORKSPACE_ROOT', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def outside(self, name, content):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return path

    def checkout(self, *parts, documents=None):
        checkout = self.root.joinpath(*parts)
        (checkout / '.git').mkdir(parents=True, exist_ok=True)
        for name, content in (documents or {}).items():
            path = checkout / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        return checkout

    def test_workspace_outside_the_tree_returns_no_context(self):
        with patch.object(file_worker, 'WORKSPACE_ROOT', self.root / 'missing'):
            self.assertEqual(file_worker.repository_context(), {})

    def test_root_checkout_reports_documents_and_adrs(self):
        self.checkout(documents={'AGENTS.md': 'ROOT RULES', 'CLAUDE.md': 'CLAUDE RULES',
                                 'docs/adr/0001-choice.md': 'ADR BODY'})
        (self.root / 'docs/agents/domain.md').mkdir(parents=True)
        context = file_worker.repository_context()
        self.assertEqual(context['repository'], '.')
        self.assertEqual(context['documents']['AGENTS.md']['content'], 'ROOT RULES')
        self.assertFalse(context['documents']['AGENTS.md']['truncated'])
        self.assertIsNone(context['documents']['AGENTS.md']['next_offset'])
        self.assertNotIn('docs/agents/domain.md', context['documents'])
        self.assertEqual(context['adrs'], ['docs/adr/0001-choice.md'])
        self.assertFalse(context['adrs_truncated'])

    def test_documents_are_truncated_at_the_per_document_limit(self):
        limit = MAX_TOOL_OUTPUT // 5
        self.checkout(documents={'AGENTS.md': 'x' * (limit + 10)})
        document = file_worker.repository_context()['documents']['AGENTS.md']
        self.assertEqual(len(document['content']), limit)
        self.assertTrue(document['truncated'])
        self.assertEqual(document['next_offset'], limit)

    def test_documents_must_stay_inside_the_checkout(self):
        checkout = self.checkout()
        outside = self.outside('outside.md', 'SECRET')
        (checkout / 'AGENTS.md').symlink_to(outside)
        documents = file_worker.repository_context()['documents']
        self.assertEqual(documents['AGENTS.md'],
                         {'error': 'document points outside the repository'})

    def test_undecodable_document_reports_its_error_type(self):
        checkout = self.checkout()
        (checkout / 'AGENTS.md').write_bytes(b'\xff\xfe\x00bad')
        documents = file_worker.repository_context()['documents']
        self.assertEqual(documents['AGENTS.md'], {'error': 'UnicodeDecodeError'})

    def test_adr_directory_outside_the_checkout_is_ignored(self):
        checkout = self.checkout()
        outside = self.outside('0002-escape.md', 'ESCAPED').parent
        (checkout / 'docs').mkdir()
        (checkout / 'docs' / 'adr').symlink_to(outside)
        self.assertEqual(file_worker.repository_context(), {})

    def test_adr_listing_error_keeps_the_readable_documents(self):
        checkout = self.checkout(documents={'AGENTS.md': 'RULES'})
        (checkout / 'docs/adr').mkdir(parents=True)
        (checkout / 'docs/adr/0003-good.md').write_text('GOOD', encoding='utf-8')
        with patch.object(file_worker.Path, 'glob', side_effect=OSError('unreadable')):
            documents = file_worker.repository_context()['documents']
        self.assertEqual(documents['AGENTS.md']['content'], 'RULES')
        self.assertEqual(documents['docs/adr'], {'error': 'OSError'})

    def test_nested_checkouts_require_an_explicit_selection(self):
        self.checkout('alpha', documents={'AGENTS.md': 'ALPHA'})
        self.checkout('beta')
        (self.root / 'plain').mkdir()
        (self.root / 'link').symlink_to(self.root / 'alpha')
        context = file_worker.repository_context()
        self.assertEqual(context['repositories'], ['alpha', 'beta'])
        self.assertTrue(context['selection_required'])
        self.assertFalse(context['truncated'])

    def test_checkout_listing_is_bounded(self):
        for index in range(33):
            self.checkout(f'repo{index:02d}')
        context = file_worker.repository_context()
        self.assertEqual(len(context['repositories']), 32)
        self.assertTrue(context['truncated'])

    def test_checkout_without_guidance_returns_no_documents(self):
        self.checkout()
        self.assertEqual(file_worker.repository_context(), {})

    def test_file_worker_main_streams_the_repository_context(self):
        self.checkout(documents={'AGENTS.md': 'RULES'})
        stdout = io.StringIO()
        with patch.object(sys, 'argv', ['file_worker.py', 'repository_context']), \
                patch.object(sys, 'stdin', io.StringIO('{}')), \
                patch.object(sys, 'stdout', stdout):
            file_worker.main()
        self.assertEqual(json.loads(stdout.getvalue())['documents']['AGENTS.md']['content'], 'RULES')


class RepositoryContextToolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / '.git').mkdir()

    def test_missing_workspace_returns_empty_guidance(self):
        with patch.object(tools, 'WORKSPACE_ROOT', Path('/nonexistent-workspace')):
            self.assertEqual(tools.read_repository_context(), '')

    def test_guidance_read_timeout_returns_empty_guidance(self):
        timeout = subprocess.TimeoutExpired('file_worker.py', 0.001)
        with patch.object(tools, 'WORKSPACE_ROOT', self.root), \
                patch.object(tools, '_file_action', side_effect=timeout):
            self.assertEqual(tools.read_repository_context(timeout_seconds=0.001), '')

    def test_guidance_read_failures_return_empty_guidance(self):
        failures = [tools.ToolExecutionError('repository_context failed with exit code 1'),
                    OSError('worker unavailable'),
                    ValueError('not json')]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), \
                    patch.object(tools, 'WORKSPACE_ROOT', self.root), \
                    patch.object(tools, '_file_action', side_effect=failure):
                self.assertEqual(tools.read_repository_context(), '')

    def test_guidance_read_recovers_after_a_transient_timeout(self):
        (self.root / 'AGENTS.md').write_text('RECOVERED_RULES', encoding='utf-8')
        worker = tools._file_action
        calls = []

        def flaky(action, arguments, timeout_seconds=None, **options):
            calls.append(action)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired('file_worker.py', 0.001)
            return worker(action, arguments, timeout_seconds, **options)

        with patch.object(tools, 'WORKSPACE_ROOT', self.root), \
                patch.object(tools, '_file_action', side_effect=flaky):
            self.assertEqual(tools.read_repository_context(), '')
            self.assertIn('RECOVERED_RULES', tools.read_repository_context())


class RunContextValidationTests(unittest.TestCase):
    def test_run_context_with_a_bare_string_scope_is_rejected(self):
        with self.assertRaises(ValueError):
            http_harness.run_http('task', run_context={'authorization': 'user text'})

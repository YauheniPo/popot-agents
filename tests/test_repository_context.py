"""Workers receive guidance from their own checkout at the time of each request."""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from popot_agents import tools
from popot_agents.worker.http_harness import run_http


class RepositoryContextTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name)
        self.requests = []

    def repository(self, name='repo', **documents):
        root = self.workspace / name
        (root / '.git').mkdir(parents=True)
        for name, content in documents.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        return root

    def run_turn(self, responses=None, *, allowed=None, state=None):
        if responses is None:
            responses = [{'content': 'done'}]
        responses = iter(responses)

        def model_response(call, **kwargs):
            self.requests.append(json.loads(call.data))
            return io.BytesIO(json.dumps({'choices': [{'message': next(responses)}]}).encode())

        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1',
                                    'HARNESS_MODEL': 'fake'}, clear=True), \
             patch.object(tools, 'WORKSPACE_ROOT', self.workspace), \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=model_response):
            return run_http('work on the repository', {
                'instructions': 'Keep the role permissions.',
                'tools': ['read_file'] if allowed is None else allowed,
                'max_tool_rounds': 3}, state=state)

    @staticmethod
    def context(request):
        return '\n'.join(message.get('content', '') for message in request['messages'])

    def test_workspace_guidance_and_adr_paths_reach_the_model(self):
        self.repository(**{
            'AGENTS.md': 'REPO_RULES: use the local checks.',
            'CLAUDE.md': 'REPO_CLAUDE: preserve contracts.',
            'GLOSSARY.md': 'REPO_TERMS: a session outlives a worker.',
            'GLOSSARY-MAP.md': 'REPO_MAP: read the relevant context.',
            'docs/agents/domain.md': 'REPO_DOMAIN: inspect relevant decisions.',
            'docs/adr/0001-choice.md': 'UNRELATED_ADR_BODY'})
        self.run_turn()
        context = self.context(self.requests[0])
        for marker in ('REPO_RULES', 'REPO_CLAUDE', 'REPO_TERMS', 'REPO_MAP', 'REPO_DOMAIN'):
            self.assertIn(marker, context)
        self.assertIn('docs/adr/0001-choice.md', context)
        self.assertNotIn('UNRELATED_ADR_BODY', context)
        self.assertIn('Keep the role permissions.', context)
        self.assertEqual([tool['function']['name'] for tool in self.requests[0]['tools']], ['read_file'])

    def test_clone_guidance_is_available_on_the_next_model_request(self):
        clone = {'tool_calls': [{'id': 'clone', 'type': 'function', 'function': {
            'name': 'git_clone', 'arguments': json.dumps({
                'url': 'https://github.com/example/repo.git', 'directory': 'repo'})}}]}

        def checkout(*args, **kwargs):
            self.repository(**{'AGENTS.md': 'FRESH_CHECKOUT_RULES'})
            return 'exit_code=0\n'

        with patch.object(tools, '_run_command', side_effect=checkout) as command:
            self.run_turn([clone, {'content': 'done'}], allowed=['git_clone', 'read_file'])
        command.assert_called_once()
        self.assertNotIn('FRESH_CHECKOUT_RULES', self.context(self.requests[0]))
        self.assertIn('FRESH_CHECKOUT_RULES', self.context(self.requests[1]))

    def test_resumed_checkpoint_reads_current_guidance_without_retaining_old_context(self):
        root = self.repository(**{'AGENTS.md': 'OLD_WORKSPACE_RULES'})
        state = {}
        report = {'content': json.dumps({'status': 'partial', 'answer': 'continue',
            'evidence': [], 'remaining_work': ['inspect the implementation'], 'errors': []})}
        self.run_turn([report], state=state)
        self.assertIn('OLD_WORKSPACE_RULES', self.context(self.requests[0]))
        self.assertNotIn('OLD_WORKSPACE_RULES', json.dumps(state))
        self.assertTrue(state['messages'])
        (root / 'AGENTS.md').write_text('CURRENT_WORKSPACE_RULES', encoding='utf-8')
        self.run_turn([report], state=state)
        self.assertIn('CURRENT_WORKSPACE_RULES', self.context(self.requests[-1]))
        self.assertNotIn('OLD_WORKSPACE_RULES', self.context(self.requests[-1]))

    def test_empty_workspace_never_imports_the_service_checkout(self):
        with tempfile.TemporaryDirectory() as service:
            (Path(service) / '.git').mkdir()
            (Path(service) / 'AGENTS.md').write_text('SERVICE_REPO_ONLY', encoding='utf-8')
            previous = Path.cwd()
            try:
                os.chdir(service)
                self.run_turn()
            finally:
                os.chdir(previous)
        self.assertNotIn('SERVICE_REPO_ONLY', self.context(self.requests[0]))

    def test_multiple_checkouts_are_listed_without_mixing_their_documents(self):
        self.repository('alpha', **{'AGENTS.md': 'ALPHA_ONLY_RULES'})
        self.repository('beta', **{'AGENTS.md': 'BETA_ONLY_RULES'})
        self.run_turn()
        context = self.context(self.requests[0])
        for name in ('alpha', 'beta'):
            self.assertIn(name, context)
        self.assertNotIn('ALPHA_ONLY_RULES', context)
        self.assertNotIn('BETA_ONLY_RULES', context)

    def test_root_checkout_is_not_confused_with_a_nested_checkout(self):
        self.repository('.', **{'AGENTS.md': 'WORKSPACE_ROOT_RULES'})
        self.repository('dependency', **{'AGENTS.md': 'DEPENDENCY_ONLY_RULES'})
        self.run_turn()
        context = self.context(self.requests[0])
        self.assertIn('WORKSPACE_ROOT_RULES', context)
        self.assertNotIn('DEPENDENCY_ONLY_RULES', context)

    def test_symlinked_documents_cannot_escape_the_checkout(self):
        root = self.repository()
        with tempfile.TemporaryDirectory() as outside:
            secret = Path(outside) / 'rules.md'
            secret.write_text('OUTSIDE_CHECKOUT_DATA', encoding='utf-8')
            (root / 'AGENTS.md').symlink_to(secret)
            self.run_turn()
        context = self.context(self.requests[0])
        self.assertNotIn('OUTSIDE_CHECKOUT_DATA', context)
        self.assertIn('AGENTS.md', context)
        self.assertIn('outside', context)

    def test_large_documents_are_bounded_and_explicitly_truncated(self):
        self.repository(**{'AGENTS.md': 'LARGE_RULES_START\n' + 'x' * 100000 + '\nLARGE_RULES_END'})
        self.run_turn()
        context = self.context(self.requests[0])
        self.assertIn('LARGE_RULES_START', context)
        self.assertNotIn('LARGE_RULES_END', context)
        self.assertIn('"truncated": true', context)
        self.assertLess(len(context), tools.MAX_TOOL_OUTPUT + 6000)

    def test_broken_guidance_links_do_not_hide_readable_documents(self):
        root = self.repository(**{'GLOSSARY.md': 'READABLE_DOMAIN_TERMS'})
        (root / 'AGENTS.md').symlink_to('AGENTS.md')
        (root / 'docs').mkdir()
        (root / 'docs/adr').symlink_to('adr')
        self.run_turn()
        context = self.context(self.requests[0])
        self.assertIn('READABLE_DOMAIN_TERMS', context)
        self.assertIn('AGENTS.md', context)
        self.assertIn('docs/adr', context)
        self.assertIn('"error":', context)

    def test_roles_without_workspace_read_capabilities_receive_no_documents(self):
        self.repository(**{'AGENTS.md': 'FILE_READER_ONLY'})
        self.run_turn(allowed=['calculate'])
        self.assertNotIn('FILE_READER_ONLY', self.context(self.requests[0]))

import io
import base64
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from scripts.check_github_token import check_write, load_mcp_writer, main


class CheckGithubTokenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = Path(self.temp.name) / '.env'
        self.env.write_text('export GITHUB_PERSONAL_ACCESS_TOKEN="test-secret"\n')

    def run_check(self, opener, *args):
        out = io.StringIO()
        with redirect_stdout(out), patch('scripts.check_github_token.build_opener', return_value=opener):
            status = main(['--env-file', str(self.env), *args])
        self.assertNotIn('test-secret', out.getvalue())
        return status, out.getvalue()

    def test_auth_and_both_repositories_are_checked_without_mutations(self):
        from unittest.mock import Mock
        opener = Mock()
        def respond(req, **kwargs):
            self.assertEqual(req.get_method(), 'GET')
            self.assertEqual(req.get_header('Authorization'), 'Bearer test-secret')
            self.assertEqual(kwargs['timeout'], 15)
            if req.full_url.endswith('/user'):
                body = {'login': 'YauheniPo'}
            elif '/contents/' in req.full_url:
                body = {'sha': 'abc', 'type': 'file'}
            else:
                body = {'default_branch': 'main'}
            return io.BytesIO(json.dumps(body).encode())
        opener.open.side_effect = respond
        status, output = self.run_check(opener)
        self.assertEqual(status, 0)
        self.assertEqual(opener.open.call_count, 5)
        self.assertIn('main', output)
        self.assertIn('Запись', output)

    def test_rejected_token_is_reported_without_error_body_or_headers(self):
        from unittest.mock import Mock
        opener = Mock()
        opener.open.side_effect = HTTPError('https://api.github.com/user', 401,
                                            'test-secret', {}, io.BytesIO(b'test-secret'))
        status, output = self.run_check(opener)
        self.assertEqual(status, 1)
        self.assertIn('401', output)
        self.assertEqual(opener.open.call_count, 1)

    def test_network_error_is_safe_and_not_success(self):
        from unittest.mock import Mock
        opener = Mock()
        opener.open.side_effect = URLError('test-secret')
        status, output = self.run_check(opener)
        self.assertEqual(status, 1)
        self.assertIn('подключения', output)

    def test_missing_token_fails_before_network(self):
        from unittest.mock import Mock
        self.env.write_text('OTHER_KEY=value\n')
        opener = Mock()
        status, output = self.run_check(opener)
        self.assertEqual(status, 2)
        opener.open.assert_not_called()

    def write_probe(self, writer_error=None, cleanup_error=False, branch_error=False):
        from unittest.mock import Mock
        opener, writer = Mock(), Mock()
        writer.side_effect = writer_error
        calls = []
        original = '# Profile\nОписание проекта 🚀\n'
        def respond(req, **kwargs):
            calls.append(req)
            url, method = req.full_url, req.get_method()
            if method == 'DELETE':
                if cleanup_error:
                    raise URLError('test-secret')
                return io.BytesIO(b'')
            if method == 'POST':
                self.assertTrue(url.endswith('/git/refs'))
                if branch_error:
                    raise HTTPError(url, 403, '', {}, io.BytesIO(b'{"message":"test-secret denied"}'))
                body = {'ref': json.loads(req.data)['ref']}
            elif '/git/ref/heads/' in url:
                self.assertTrue(url.endswith('release%2Fprofile'))
                body = {'object': {'sha': 'head-sha'}}
            elif '/contents/' in url:
                content = original
                if writer.called:
                    # GitHub MCP accepts plaintext; only the REST response is base64.
                    content = writer.call_args.args[1]['content']
                body = {'sha': 'file-sha', 'encoding': 'base64',
                        'content': base64.b64encode(content.encode()).decode()}
            elif url.endswith('/user'):
                body = {'login': 'YauheniPo'}
            else:
                body = {'default_branch': 'release/profile'}
            return io.BytesIO(json.dumps(body).encode())
        opener.open.side_effect = respond
        with patch('scripts.check_github_token.load_mcp_writer', return_value=writer, create=True):
            status, output = self.run_check(opener, '--check-write')
        return status, output, calls, writer

    def test_write_uses_mcp_on_temporary_branch_and_verifies_and_deletes_it(self):
        status, output, calls, writer = self.write_probe()
        self.assertEqual(status, 0)
        writer.assert_called_once()
        token, arguments = writer.call_args.args
        self.assertEqual(token, 'test-secret')
        self.assertEqual(arguments['owner'], 'YauheniPo')
        self.assertEqual(arguments['repo'], 'YauheniPo')
        self.assertEqual(arguments['sha'], 'file-sha')
        self.assertEqual(arguments['path'], 'README.md')
        self.assertEqual(arguments['content'], '# Profile\nОписание проекта 🚀\n'
                         f"\n<!-- {arguments['branch']}: GitHub MCP write probe -->\n")
        self.assertTrue(arguments['branch'].startswith('popot-token-check-'))
        self.assertIn('Assisted-by:', arguments['message'])
        self.assertEqual(calls[-1].get_method(), 'DELETE')
        self.assertIn('подтверждена', output)

    def test_mcp_denied_write_shows_original_error_and_cleans_branch(self):
        status, output, calls, writer = self.write_probe(
            RuntimeError('403 Resource not accessible by personal access token test-secret'))
        self.assertEqual(status, 1)
        self.assertIn('create_or_update_file', output)
        self.assertIn('403 Resource not accessible', output)
        self.assertEqual(calls[-1].get_method(), 'DELETE')
        self.assertNotIn('подтверждена', output)

    def test_cleanup_failure_is_reported_and_returns_failure(self):
        status, output, calls, writer = self.write_probe(cleanup_error=True)
        self.assertEqual(status, 1)
        self.assertIn('удалить вручную', output)

    def test_branch_failure_does_not_claim_mcp_write_was_attempted_or_delete_branch(self):
        status, output, calls, writer = self.write_probe(branch_error=True)
        self.assertEqual(status, 1)
        writer.assert_not_called()
        self.assertIn('403', output)
        self.assertFalse(any(req.get_method() == 'DELETE' for req in calls))

    def test_missing_sdk_cannot_create_a_branch(self):
        from unittest.mock import Mock
        request, emit = Mock(), Mock()
        with patch('scripts.check_github_token.load_mcp_writer', side_effect=ImportError):
            self.assertFalse(check_write(request, 'test-secret', emit))
        request.assert_not_called()

    def test_sdk_calls_exact_tool_and_preserves_declared_error(self):
        from contextlib import asynccontextmanager
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock
        session = SimpleNamespace(initialize=AsyncMock(), call_tool=AsyncMock(
            return_value=SimpleNamespace(isError=True, content=[SimpleNamespace(
                type='text', text='403 Resource not accessible by personal access token')])) )
        @asynccontextmanager
        async def context(value):
            yield value
        client_factory = Mock(side_effect=lambda **kwargs: context('client'))
        streams_factory = Mock(side_effect=lambda *a, **kw: context(('read', 'write', None)))
        session_factory = Mock(side_effect=lambda *a: context(session))
        modules = {'httpx': SimpleNamespace(AsyncClient=client_factory),
                   'mcp': SimpleNamespace(ClientSession=session_factory),
                   'mcp.client': SimpleNamespace(),
                   'mcp.client.streamable_http': SimpleNamespace(streamable_http_client=streams_factory)}
        with patch.dict('sys.modules', modules):
            writer = load_mcp_writer()
            with self.assertRaisesRegex(RuntimeError, '403 Resource not accessible'):
                writer('test-secret', {'repo': 'YauheniPo'})
        session.initialize.assert_awaited_once()
        session.call_tool.assert_awaited_once_with('create_or_update_file', {'repo': 'YauheniPo'})
        self.assertFalse(client_factory.call_args.kwargs['follow_redirects'])

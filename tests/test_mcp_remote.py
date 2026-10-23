import asyncio
import json
import os
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from popot_agents.orchestrator.main import load_agents, load_roles
from popot_agents.worker import mcp_client

ROOT = Path(__file__).resolve().parents[1]
REMOTE = {"url": "https://api.githubcopilot.com/mcp/",
          "bearer_token_env": "GITHUB_PERSONAL_ACCESS_TOKEN", "tools": ["get_file_contents"]}


class RemoteMCPTests(unittest.TestCase):
    def test_remote_config_requires_https_and_role_selected_token(self):
        roles = json.loads((ROOT / 'config/roles.json').read_text())
        role = roles['backend_engineer']
        role['mcpServers']['github'] = dict(REMOTE)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'roles.json'
            path.write_text(json.dumps(roles))
            agents = load_agents(ROOT / 'config/agents.json')
            load_roles(path, agents)
            for changes in ({'url': 'http://example.com/mcp'},
                            {'url': 'https://user:secret@example.com/mcp'},
                            {'url': 'https://example.com/mcp?token=secret'},
                            {'bearer_token_env': 'OPENROUTER_API_KEY'},
                            {'command': ['curl']}):
                with self.subTest(changes=changes):
                    role['mcpServers']['github'] = dict(REMOTE, **changes)
                    path.write_text(json.dumps(roles))
                    with self.assertRaises(ValueError):
                        load_roles(path, agents)

    def test_remote_discovery_paginates_and_call_uses_env_auth(self):
        events = []
        outer = self
        @asynccontextmanager
        async def http_client(**kwargs):
            self.assertEqual(kwargs['headers'], {'Authorization': 'Bearer test-secret'})
            self.assertFalse(kwargs['follow_redirects'])
            events.append('http-open')
            yield 'http'
            events.append('http-close')
        @asynccontextmanager
        async def transport(url, *, http_client):
            self.assertEqual(url, REMOTE['url'])
            self.assertEqual(http_client, 'http')
            yield ('read', 'write')
        class Session:
            def __init__(self, *args): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def initialize(self): events.append('initialize')
            async def list_tools(self, *, params=None):
                if params is None:
                    return SimpleNamespace(tools=[], next_cursor='page2')
                outer.assertEqual(params.cursor, 'page2')
                return SimpleNamespace(tools=[SimpleNamespace(name='get_file_contents',
                    description='Read a file', input_schema={'type': 'object'})], next_cursor=None)
            async def call_tool(self, name, arguments):
                events.append((name, arguments))
                return SimpleNamespace(content=[
                    SimpleNamespace(type='text', text='Downloaded file SHA: abc'),
                    SimpleNamespace(type='resource', resource=SimpleNamespace(
                        uri='repo://owner/repo/README.md', text='# README contents'))], is_error=False)
        modules = {'httpx2': SimpleNamespace(AsyncClient=http_client),
                   'mcp': SimpleNamespace(ClientSession=Session, types=SimpleNamespace(
                       PaginatedRequestParams=lambda **kwargs: SimpleNamespace(**kwargs))),
                   'mcp.client': SimpleNamespace(),
                   'mcp.client.streamable_http': SimpleNamespace(streamable_http_client=transport)}
        with patch.dict(sys.modules, modules), patch.dict(os.environ, {
                'HARNESS_TOOL_ENV_NAMES_JSON': '["GITHUB_PERSONAL_ACCESS_TOKEN"]',
                'GITHUB_PERSONAL_ACCESS_TOKEN': 'test-secret'}):
            discovered = mcp_client.discover_tools({'github': REMOTE})
            self.assertEqual(set(discovered), {'mcp__github__get_file_contents'})
            output = mcp_client.call_tool(REMOTE, 'get_file_contents', {'repo': 'example'})
            self.assertIn('SHA: abc', output)
            self.assertIn('# README contents', output)
            self.assertIn('repo://owner/repo/README.md', output)
        self.assertEqual(events.count('http-close'), 2)
        self.assertIn(('get_file_contents', {'repo': 'example'}), events)
        self.assertNotIn('test-secret', json.dumps(discovered))

    def test_missing_or_unselected_token_fails_before_connect(self):
        with patch.dict(os.environ, {'HARNESS_TOOL_ENV_NAMES_JSON': '[]',
                                     'GITHUB_PERSONAL_ACCESS_TOKEN': 'test-secret'}):
            with self.assertRaisesRegex(RuntimeError, 'GITHUB_PERSONAL_ACCESS_TOKEN'):
                mcp_client.discover_tools({'github': REMOTE})

    def test_remote_error_redacts_token(self):
        async def fail(*args):
            raise RuntimeError('upstream echoed test-secret')
        with patch.dict(os.environ, {
                'HARNESS_TOOL_ENV_NAMES_JSON': '["GITHUB_PERSONAL_ACCESS_TOKEN"]',
                'GITHUB_PERSONAL_ACCESS_TOKEN': 'test-secret'}), \
             patch.object(mcp_client, '_call_one', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, r'\[REDACTED\]') as raised:
                mcp_client.call_tool(REMOTE, 'get_file_contents', {})
            self.assertNotIn('test-secret', str(raised.exception))
            self.assertTrue(raised.exception.__suppress_context__)

    def test_nested_transport_errors_expose_cause_without_token(self):
        # anyio's TaskGroup wraps HTTP transport failures, including on Python 3.10.
        class TransportGroup(Exception):
            def __init__(self, *errors):
                super().__init__('unhandled errors in a TaskGroup')
                self.exceptions = errors

        async def fail(*args):
            raise TransportGroup(TransportGroup(RuntimeError(
                'HTTP 503 from upstream, Authorization: Bearer test-secret')))

        with patch.dict(os.environ, {
                'HARNESS_TOOL_ENV_NAMES_JSON': '["GITHUB_PERSONAL_ACCESS_TOKEN"]',
                'GITHUB_PERSONAL_ACCESS_TOKEN': 'test-secret'}):
            for operation, target in (
                    (lambda: mcp_client.discover_tools({'github': REMOTE}), '_discover_one'),
                    (lambda: mcp_client.call_tool(REMOTE, 'get_file_contents', {}), '_call_one')):
                with self.subTest(target=target), patch.object(mcp_client, target, side_effect=fail):
                    with self.assertRaisesRegex(RuntimeError, 'HTTP 503') as raised:
                        operation()
                    self.assertIn('[REDACTED]', str(raised.exception))
                    self.assertNotIn('test-secret', str(raised.exception))
                    self.assertTrue(raised.exception.__suppress_context__)

    def test_remote_call_honors_remaining_deadline(self):
        closed = []
        async def wait(*args):
            try:
                await asyncio.sleep(30)
            finally:
                closed.append(True)
        with patch.object(mcp_client, '_call_one', side_effect=wait):
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                mcp_client.call_tool(REMOTE, 'get_file_contents', {}, timeout_seconds=0.01)
        self.assertEqual(closed, [True])

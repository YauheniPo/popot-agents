"""Exercise the worker's real SDK transports and bundled stdio servers."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from popot_agents.worker import mcp_client


class BundledMCPTests(unittest.TestCase):
    def test_fetch_discovery_and_invalid_input_use_the_real_stdio_server(self):
        config = {'command': [sys.executable, '-m', 'mcp_server_fetch'], 'tools': ['fetch']}
        tools = mcp_client.discover_tools({'fetch': config}, timeout_seconds=15)
        self.assertIn('url', tools['mcp__fetch__fetch']['schema']['function']['parameters']['properties'])
        with self.assertRaises(mcp_client.MCPToolError):
            mcp_client.call_tool(config, 'fetch', {'url': 'not-a-url'}, timeout_seconds=15)

    def test_git_discovery_and_diff_use_the_real_stdio_server(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(['git', 'init', '-q'], cwd=directory, check=True, capture_output=True)
            path = Path(directory) / 'example.txt'
            path.write_text('before\n')
            subprocess.run(['git', 'add', 'example.txt'], cwd=directory, check=True, capture_output=True)
            path.write_text('after\n')
            config = {'command': [sys.executable, '-m', 'mcp_server_git', '--repository', directory],
                      'tools': ['git_status', 'git_diff_unstaged']}
            tools = mcp_client.discover_tools({'git': config}, timeout_seconds=15)
            self.assertEqual(set(tools), {'mcp__git__git_status', 'mcp__git__git_diff_unstaged'})
            status = mcp_client.call_tool(config, 'git_status', {'repo_path': directory}, timeout_seconds=15)
            self.assertIn('example.txt', status)
            diff = mcp_client.call_tool(config, 'git_diff_unstaged', {'repo_path': directory}, timeout_seconds=15)
            self.assertIn('-before', diff)
            self.assertIn('+after', diff)

    def test_memory_discovery_uses_the_real_stdio_server(self):
        path = Path(__file__).resolve().parents[1] / 'popot_agents' / 'worker' / 'cognee_memory.py'
        config = {'command': [sys.executable, str(path)],
                  'tools': ['remember', 'recall', 'memory_status']}
        tools = mcp_client.discover_tools({'cognee': config}, timeout_seconds=15)
        self.assertEqual(set(tools), {'mcp__cognee__remember', 'mcp__cognee__recall',
                                     'mcp__cognee__memory_status'})

    def test_cognee_stdio_discovery_and_validation_without_memory_service(self):
        adapter = Path(mcp_client.__file__).with_name('cognee_memory.py')
        config = {'command': [sys.executable, str(adapter)],
                  'tools': ['recall', 'memory_status']}
        tools = mcp_client.discover_tools({'cognee': config}, timeout_seconds=10)
        self.assertEqual(set(tools), {'mcp__cognee__recall', 'mcp__cognee__memory_status'})
        schema = tools['mcp__cognee__recall']['schema']['function']['parameters']
        self.assertIn('query', schema['required'])
        # Missing required input must remain a tool error, without calling Cognee.
        with self.assertRaises(mcp_client.MCPToolError):
            mcp_client.call_tool(config, 'recall', {}, timeout_seconds=10)

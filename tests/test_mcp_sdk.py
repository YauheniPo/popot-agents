"""Exercise the shipped stdio adapter through the real MCP SDK."""
import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from popot_agents.worker import mcp_client


@unittest.skipUnless(importlib.util.find_spec('mcp'), 'requires the installed MCP SDK')
class MCPSDKTests(unittest.TestCase):
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

    @unittest.skipUnless(shutil.which('mcp-server-git'), 'requires the isolated mcp-tools group')
    def test_git_stdio_fallback_and_tool_call(self):
        config = {'command': ['mcp-server-git'], 'tools': ['git_status', 'git_diff_unstaged']}
        tools = mcp_client.discover_tools({'git': config}, timeout_seconds=10)
        self.assertEqual(set(tools), {'mcp__git__git_status', 'mcp__git__git_diff_unstaged'})
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(['git', 'init', '--quiet', directory], check=True)
            Path(directory, 'mcp-fixture.txt').write_text('untracked file')
            result = mcp_client.call_tool(config, 'git_status', {'repo_path': directory}, timeout_seconds=10)
        self.assertIn('mcp-fixture.txt', result)

    @unittest.skipUnless(shutil.which('mcp-server-fetch'), 'requires the isolated mcp-tools group')
    def test_fetch_stdio_fallback_and_validation_without_network(self):
        config = {'command': ['mcp-server-fetch'], 'tools': ['fetch']}
        tools = mcp_client.discover_tools({'fetch': config}, timeout_seconds=10)
        self.assertEqual(set(tools), {'mcp__fetch__fetch'})
        with self.assertRaises(mcp_client.MCPToolError):
            mcp_client.call_tool(config, 'fetch', {}, timeout_seconds=10)

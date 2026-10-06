"""Tool-declared failures must return to the model; transport failures stay fatal."""
import io
import json
import os
import sys
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

from popot_agents.worker import mcp_client
from popot_agents.worker.http_harness import run_http


class MCPRecoveryTests(unittest.TestCase):
    @asynccontextmanager
    async def streams(self, config):
        yield None, None

    def sdk(self):
        outer = self
        class Session:
            def __init__(self, *args): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def initialize(self): pass
            async def call_tool(self, name, arguments):
                return SimpleNamespace(isError=True, content=[SimpleNamespace(
                    type='text', text=outer.error_message)])
        return SimpleNamespace(ClientSession=Session)

    def test_error_result_retains_type_and_redacts_before_truncation(self):
        self.error_message = 'x' * 30 + 'private-token' + ': ref master missing'
        with patch.dict(sys.modules, {'mcp': self.sdk()}), \
             patch.object(mcp_client, '_streams', self.streams), \
             patch.object(mcp_client, 'MAX_TOOL_OUTPUT', 35), \
             patch.object(mcp_client, 'safe_tool_env', return_value={'TOKEN': 'private-token'}):
            with self.assertRaises(mcp_client.MCPToolError) as caught:
                mcp_client.call_tool({'bearer_token_env': 'TOKEN'}, 'get_file_contents', {})
        self.assertLessEqual(len(str(caught.exception)), 35)
        self.assertNotIn('priva', str(caught.exception))

    def run_turn(self, effects, *, rounds=4):
        self.requests = []
        aliases = ['mcp__github__get_file_contents', 'mcp__github__list_branches']
        tools = {name: {'server': 'github', 'native_name': name.split('__')[-1],
                       'schema': {'type': 'function', 'function': {'name': name,
                                  'parameters': {'type': 'object'}}}} for name in aliases}
        def tool_message(name, args, call_id):
            return {'content': None, 'tool_calls': [{'id': call_id, 'type': 'function',
                    'function': {'name': name, 'arguments': json.dumps(args)}}]}
        model_answers = [tool_message(aliases[0], {'ref': 'master'}, 'call1'),
                         tool_message(aliases[1], {'repo': 'example'}, 'call2'),
                         tool_message(aliases[0], {'ref': 'refs/heads/main'}, 'call3'),
                         {'content': 'Read README successfully.'}]
        def urlopen(req, **kwargs):
            self.requests.append(json.loads(req.data))
            return io.BytesIO(json.dumps({'choices': [{'message': model_answers.pop(0)}]}).encode())
        with patch.dict(os.environ, {'HARNESS_BASE_URL': 'http://fake/v1', 'HARNESS_MODEL': 'fake'}), \
             patch.object(mcp_client, 'discover_tools', return_value=tools), \
             patch.object(mcp_client, 'call_tool', side_effect=effects) as call, \
             patch('popot_agents.worker.http_harness.request.urlopen', side_effect=urlopen):
            result = run_http('Read README', {'tools': [], 'mcpServers': {'github': {}},
                                             'max_tool_rounds': rounds})
        return result, call

    def test_model_can_recover_after_missing_ref(self):
        result, call = self.run_turn([mcp_client.MCPToolError('could not resolve ref master'),
                                      '[{"name":"main"}]', '# README'])
        self.assertEqual(result, 'Read README successfully.')
        self.assertEqual(call.call_count, 3)
        error = self.requests[1]['messages'][-1]
        self.assertEqual(error['role'], 'tool')
        self.assertEqual(error['tool_call_id'], 'call1')
        self.assertTrue(json.loads(error['content'])['isError'])
        self.assertIn('master', json.loads(error['content'])['error'])
        self.assertEqual(self.requests[3]['messages'][-1]['content'], '# README')

    def test_transport_failure_is_not_retried_as_tool_rejection(self):
        with self.assertRaisesRegex(RuntimeError, 'transport failed'):
            self.run_turn([RuntimeError('transport failed')])
        self.assertEqual(len(self.requests), 1)

    def test_rejected_tools_still_consume_round_budget(self):
        with self.assertRaisesRegex(RuntimeError, 'tool-call limit'):
            self.run_turn([mcp_client.MCPToolError('missing ref')], rounds=1)
        self.assertEqual(len(self.requests), 1)

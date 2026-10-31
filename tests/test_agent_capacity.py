import asyncio
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from popot_agents import tools
from popot_agents.orchestrator.main import load_agents, load_roles
from popot_agents.runtime_config import RUNTIME
from popot_agents.worker import mcp_client
from popot_agents.worker.http_harness import _model_request
from popot_agents.worker.session_worker import Conversation
from ax_local.config import AX_CONFIG
from ax_local.api.ax_runner import AxChat
from ax_local.api.delegation import DelegationService

ROOT = Path(__file__).resolve().parents[1]


class AgentCapacityTests(unittest.TestCase):
    def test_model_timeout_does_not_repeat_the_gateway_fallback_chain(self):
        timeouts = []
        def respond(call, timeout):
            timeouts.append(timeout)
            if len(timeouts) == 1:
                raise TimeoutError()
            return io.BytesIO(b'{"choices": []}')
        with patch('popot_agents.worker.http_harness.request.urlopen', side_effect=respond):
            with self.assertRaisesRegex(RuntimeError, 'model request timed out'):
                _model_request(object(), round_number=1, messages=[], schemas=[], remaining_seconds=lambda: 500)
        self.assertEqual(timeouts, [120])

    def test_large_mcp_output_is_preserved_and_can_be_read_past_preview(self):
        content = 'начало\n' + 'x' * 30000 + '\nimportant finding at the end; continue with read_file '
        class Client:
            def __init__(self, *_): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def call_tool(self, *args, **kwargs):
                return SimpleNamespace(content=[SimpleNamespace(type='text', text=content)], is_error=False)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(tools, 'WORKSPACE_ROOT', Path(directory)), \
             patch.dict(sys.modules, {'mcp': SimpleNamespace(Client=Client)}):
            preview = asyncio.run(mcp_client._call_one({}, 'get_diff', {}))
            match = re.search(r'Full output: (\.tool-results/[a-f0-9]+\.txt)', preview)
            self.assertIsNotNone(match, preview[-200:])
            path = match.group(1)
            self.assertEqual((Path(directory) / path).read_text(), content)
            page = tools.execute_tool('read_tool_output', {'path': path, 'offset': 30007, 'limit': 100}, ['read_tool_output'])
            self.assertEqual(page, content[30007:])
            self.assertLess(len(preview), tools.MAX_TOOL_OUTPUT + 500)

    def test_saved_output_paths_are_checked_as_workspace_user(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tools, 'WORKSPACE_ROOT', Path(directory)):
            preview = tools.preserve_tool_output('x' * 30000 + 'TAIL')
            path = re.search(r'Full output: (.+)', preview).group(1)
            # AX root cannot inspect UID 10001's private artifact directory.
            # Validation must run inside the file worker under that same UID.
            with patch.object(tools, 'workspace_path', side_effect=PermissionError()), \
                 patch.object(Path, 'is_symlink', side_effect=PermissionError()):
                self.assertEqual(tools.execute_tool('read_tool_output',
                    {'path': path, 'offset': 30000}, ['read_tool_output']), 'TAIL')

    def test_saved_output_reader_rejects_symlinked_file_or_directory(self):
        for directory_link in (False, True):
            with self.subTest(directory_link=directory_link), tempfile.TemporaryDirectory() as directory, \
                 patch.object(tools, 'WORKSPACE_ROOT', Path(directory)):
                root = Path(directory)
                artifacts = root / '.tool-results'
                if directory_link:
                    (root / 'other').mkdir()
                    artifacts.symlink_to(root / 'other', target_is_directory=True)
                    (root / 'other' / ('a' * 32 + '.txt')).write_text('private')
                else:
                    artifacts.mkdir()
                    (root / 'other.txt').write_text('private')
                    (artifacts / ('a' * 32 + '.txt')).symlink_to(root / 'other.txt')
                with self.assertRaises((tools.ToolInputError, tools.ToolExecutionError)):
                    tools.execute_tool('read_tool_output',
                        {'path': '.tool-results/' + 'a' * 32 + '.txt'}, ['read_tool_output'])

    def test_file_paging_does_not_split_unicode_or_lose_tail(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tools, 'WORKSPACE_ROOT', Path(directory)):
            (Path(directory) / 'text.txt').write_text('абвгдежз')
            first = tools.execute_tool('read_file', {'path': 'text.txt', 'offset': 2, 'limit': 3}, ['read_file'])
            self.assertTrue(first.startswith('вгд'))
            self.assertIn('offset=5', first)
            self.assertEqual(tools.execute_tool('read_file', {'path': 'text.txt', 'offset': 5, 'limit': 3}, ['read_file']), 'ежз')
            for args in ({'offset': -1}, {'offset': True}, {'limit': 0}, {'limit': tools.MAX_TOOL_OUTPUT + 1}):
                with self.subTest(args=args), self.assertRaises(tools.ToolInputError):
                    tools.execute_tool('read_file', {'path': 'text.txt', **args}, ['read_file'])

    def test_history_over_old_limit_survives_restore_and_next_turn(self):
        conversation = Conversation(lambda messages: 'done')
        history = [{'role': 'user', 'content': 'review'}, {'role': 'assistant', 'content': 'x' * 600000}]
        conversation.restore(history)
        self.assertEqual(conversation.ask('continue'), 'done')
        payload = json.dumps({'messages': conversation.messages, 'checkpoint': {'messages': history}}).encode()
        self.assertLess(len(payload), RUNTIME['limits']['socket_message_bytes'])
        self.assertGreaterEqual(AX_CONFIG['proxy']['max_request_bytes'], 2 * RUNTIME['limits']['history_bytes'])

    def test_reviewer_can_run_checks_and_delegate_qa(self):
        role = load_roles(ROOT / 'config/roles', load_agents(ROOT / 'config/agents.json'))['code_reviewer']
        self.assertTrue(role['permissions']['shell'])
        self.assertTrue({'bash', 'read_file', 'write_file', 'git_clone'} <= set(role['tools']))
        self.assertTrue({'qa_engineer', 'analyst'} <= set(role['allowed_roles']))

    def test_suspended_sandbox_survives_two_hours_but_expires_after_one_day(self):
        worker = AxChat(runner=None, container_name='test', timeout_seconds=900)
        service = DelegationService({}, {}, {}, None, 'http://local')
        service.retained['test'] = (worker, 0)
        with patch('ax_local.api.delegation.time.monotonic', return_value=3 * 3600), \
             patch.object(worker, 'close') as close:
            service.maintenance()
            close.assert_not_called()
        self.assertIn('test', service.retained)
        with patch('ax_local.api.delegation.time.monotonic', return_value=24 * 3600 + 1), \
             patch.object(worker, 'close') as close:
            service.maintenance()
            close.assert_called_once()
        self.assertNotIn('test', service.retained)

    def test_saved_output_reader_cannot_read_arbitrary_workspace_files(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tools, 'WORKSPACE_ROOT', Path(directory)):
            (Path(directory) / 'private.txt').write_text('not a tool artifact')
            for path in ('private.txt', '.tool-results/../private.txt'):
                with self.subTest(path=path), self.assertRaises(tools.ToolInputError):
                    tools.execute_tool('read_tool_output', {'path': path}, ['read_tool_output'])

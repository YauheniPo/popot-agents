"""Unit contract for the Cognee session bridge without a running Cognee service."""

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


class SessionApiTests(unittest.TestCase):
    def test_first_project_session_creates_owned_dataset_before_recording(self):
        app = SimpleNamespace(post=lambda _path: lambda handler: handler)
        manager = SimpleNamespace(add_agent_trace_step=AsyncMock())
        get_datasets = AsyncMock(return_value=[])
        create_dataset = AsyncMock(return_value=SimpleNamespace(id='project-id'))
        ensure_session = AsyncMock()
        dependencies = {
            'fastapi': {'Depends': lambda dependency: dependency},
            'pydantic': {'BaseModel': type('BaseModel', (), {}),
                         'ConfigDict': lambda **kwargs: kwargs,
                         'Field': lambda default=None, **kwargs: default},
            'cognee.api.client': {'app': app},
            'cognee.infrastructure.session.get_session_manager':
                {'get_session_manager': Mock(return_value=manager)},
            'cognee.modules.data.methods':
                {'get_datasets_by_name': get_datasets,
                 'create_authorized_dataset': create_dataset},
            'cognee.modules.session_lifecycle.metrics':
                {'SessionStatus': lambda value: value,
                 'accumulate_usage': AsyncMock(),
                 'ensure_and_touch_session': ensure_session,
                 'get_session_row': AsyncMock(return_value=None),
                 'mark_ended': AsyncMock()},
            'cognee.modules.users.methods':
                {'get_authenticated_user': lambda: None},
            'cognee.modules.users.models': {'User': type('User', (), {})},
        }
        modules = {}
        for name, attributes in dependencies.items():
            module = ModuleType(name)
            module.__dict__.update(attributes)
            modules[name] = module

        path = Path(__file__).resolve().parents[1] / 'memory' / 'session_api.py'
        spec = importlib.util.spec_from_file_location('_session_api_test', path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(module)
            payload = SimpleNamespace(session_id='popot-test-123',
                dataset_name='github-owner-repo', role='agent', status='running',
                tools=[SimpleNamespace(name='cognee.recall', status='success',
                    output_chars=237, recall_outcome='warming_up', chunks=0)] + [
                    SimpleNamespace(name='bash', status='success', output_chars=i,
                                    recall_outcome=None, chunks=None) for i in range(199)],
                model='', tokens_in=0, tokens_out=0, cost_usd=0,
                cost_reported=False, memories_saved=0)
            user = SimpleNamespace(id='user-id')
            asyncio.run(module.session_event(payload, user))

        create_dataset.assert_awaited_once_with(payload.dataset_name, user)
        ensure_session.assert_awaited_once_with(
            session_id=payload.session_id, user_id=user.id, dataset_id='project-id')

        trace = manager.add_agent_trace_step.call_args_list[0].kwargs
        self.assertEqual(manager.add_agent_trace_step.await_count, 2)
        self.assertEqual(trace['origin_function'], 'popot.tools')
        batches = trace['method_return_value']['tool_batches']
        self.assertLessEqual(len(batches), 20)
        self.assertTrue(all(len(batch) <= 20 for batch in batches))
        steps = [step for batch in batches for step in batch]
        self.assertEqual(len(steps), 200)
        self.assertEqual(steps[0], {'name': 'cognee.recall', 'status': 'success',
            'output_chars': 237, 'recall_outcome': 'warming_up', 'chunks': 0})
        self.assertEqual([step['output_chars'] for step in steps[1:]], list(range(199)))

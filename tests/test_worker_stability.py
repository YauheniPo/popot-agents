"""Regression cases from the exhausted PR #37 workflow."""
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from popot_agents.worker import http_harness
from popot_agents.orchestrator.workflow import WorkflowRun
from popot_agents.workflow_config import POLICY
from ax_local.api.worker_queue import WorkerQueue
from ax_local.worker import entrypoint
from tests import test_workflow as workflows
from tests.test_workflow import result


class StabilityTests(unittest.TestCase):
    def test_tiny_turn_is_rejected_before_creating_a_memory_session(self):
        with patch.object(http_harness, 'TurnMemory') as memory, \
             patch.object(http_harness, '_run_http', return_value='unexpected') as execution:
            with self.assertRaisesRegex(ValueError, 'budget'):
                http_harness.run_http('fixture', timeout_seconds=.8)
        memory.assert_not_called()
        execution.assert_not_called()

    def test_independent_leaf_children_fit_beside_parent_in_three_worker_pool(self):
        from ax_local.api.delegation import DelegationService, Run, Job
        from types import SimpleNamespace
        pool = WorkerQueue(3, 1)
        pool.acquire('root'); pool.reserve('run', 2)
        run = Run(time.monotonic() + 10, pool=pool, reservation='run', reserved_slots=2)
        def child(sid):
            return Job(sid, {'allowed_roles': [], '_ax_delegation': {'lineage': ['parent', sid]}},
                       'fixture', run, run.deadline)
        first = child('a' * 16); run.admitted[first.session_id] = first
        service = SimpleNamespace(policy={'max_depth': 2})
        self.assertTrue(DelegationService._can_start(service, child('b' * 16)))

    def test_child_deadline_leaves_parent_a_finalization_window(self):
        from ax_local.tests import test_delegation as delegation_tests
        helper = delegation_tests.DelegationTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        deadline = time.monotonic() + 600
        with helper.service.workflow(helper.parent, deadline, max_turns=4, metadata={'turns': 1}), \
             helper.service.turn(helper.parent, 600):
            submitted = helper.service.dispatch(helper.token, 'backend_engineer', 'SendMessage', {
                'message': {'messageId': 'reserved-finalization', 'role': 'ROLE_USER',
                            'parts': [{'text': 'bounded work'}]},
                'configuration': {'returnImmediately': True}})
            saved = helper.store.get(submitted['task']['id'])
            self.assertGreaterEqual(time.time() + 600 - saved['roleConfig']['_ax_delegation']['deadlineAt'], 119)

    def test_startup_failure_exposes_known_reason_without_arbitrary_exception_text(self):
        from ax_local.api.delegation import _worker_failure
        from popot_agents.orchestrator.main import AgentRunError
        reason, failure = _worker_failure(AgentRunError('ActorResumeFailed private-value'), 'worker_startup')
        self.assertEqual(failure['code'], 'actor_resume_failed')
        self.assertIn('capacity', reason)
        self.assertNotIn('private-value', reason)

    def test_ax_lookup_failure_identifies_control_plane_without_leaking_detail(self):
        from ax_local.api.delegation import _worker_failure
        from popot_agents.orchestrator.main import AgentRunError
        reason, failure = _worker_failure(AgentRunError('AX task lookup failed'), 'worker_startup')
        self.assertEqual(failure['code'], 'ax_task_lookup_failed')
        self.assertIn('AX task lookup', reason)

    def test_connection_reset_is_reported_as_transport_interruption(self):
        from ax_local.api.delegation import _worker_failure
        reason, failure = _worker_failure(ConnectionResetError('private endpoint'), 'worker_message')
        self.assertEqual(failure['code'], 'worker_transport_interrupted')
        self.assertIn('reconcile', reason)
        self.assertNotIn('private endpoint', reason)

    def test_wrapped_worker_connection_reset_keeps_transport_reason(self):
        from ax_local.api.delegation import _worker_failure
        from popot_agents.orchestrator.main import AgentRunError
        reason, failure = _worker_failure(AgentRunError('[Errno 104] Connection reset by peer'), 'worker_message')
        self.assertEqual(failure['code'], 'worker_transport_interrupted')
        self.assertIn('reconcile', reason)

    def test_memory_capture_records_summary_and_save_timeouts_separately(self):
        from popot_agents.worker.turn_memory import TurnMemory
        for phase in ('summary', 'save'):
            with self.subTest(phase=phase), patch.dict(os.environ, {
                    'COGNEE_PASSWORD': 'fixture-password',
                    'HARNESS_TOOL_ENV_NAMES_JSON': '["COGNEE_PASSWORD"]'}):
                memory = TurnMemory({'memory_project': 'fixture', 'mcpServers': {'cognee': {}}},
                                    [{'role': 'user', 'content': 'fixture'}], client=Mock())
                memory.capture_failed(TimeoutError(), phase=phase)
                self.assertEqual(memory.tools[-1]['phase'], phase)
                self.assertEqual(memory.tools[-1]['error_type'], 'TimeoutError')

    def test_tiny_model_budget_does_not_make_a_network_request(self):
        response = io.BytesIO(json.dumps({'choices': [{'message': {'content': 'unexpected'}}]}).encode())
        with patch.object(http_harness.request, 'urlopen', return_value=response) as network:
            with self.assertRaisesRegex(RuntimeError, 'budget'):
                http_harness._model_request(None, round_number=1, messages=[], schemas=[],
                                           remaining_seconds=lambda: .08, final=True)
        network.assert_not_called()

    def test_partial_does_not_start_another_turn_with_one_second_left(self):
        run = WorkflowRun(POLICY, 'task')
        with patch.object(run, 'remaining', return_value=1.1):
            stopped = run.record(result('partial', remaining=('verify',)), {'messages': []})
        self.assertIsNotNone(stopped)
        self.assertEqual(stopped['outcome']['reason'], 'workflow_deadline_exhausted')

    def test_coordinator_timeout_returns_saved_evidence_instead_of_http_504(self):
        calls, response, saved = workflows.WorkflowTests().run_workflow([
            result('partial', evidence=('commit abc verified',), remaining=('QA',)),
            TimeoutError('deadline exceeded')], allow_error=True)
        self.assertIn('outcome', response)
        self.assertEqual(response['outcome']['status'], 'blocked')
        self.assertIn('commit abc verified', response['outcome']['evidence'])
        self.assertEqual(saved['roleConfig']['_workflow']['state'], 'blocked')

    def test_three_worker_pool_reserves_a_path_to_nested_verification(self):
        queue = WorkerQueue(3, 1)
        queue.acquire('root')
        self.assertEqual(queue.reserve('workflow', 2), 2)
        queue.acquire('implementation', reservation='workflow')
        queue.acquire('verification', reservation='workflow')
        self.assertEqual(queue.status()['active'], 3)

    def test_directory_guard_rejects_symlinks_even_if_open_ignores_nofollow(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'target'; target.mkdir(mode=0o755)
            link = Path(directory) / 'link'; link.symlink_to(target)
            original = target.stat()
            real_open = os.open
            with patch.object(entrypoint.os, 'open', side_effect=lambda path, flags:
                              real_open(path, flags & ~os.O_NOFOLLOW)):
                with self.assertRaises(OSError):
                    entrypoint.prepare_directory(str(link), tool_user=False)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), stat.S_IMODE(original.st_mode))

    def test_directory_replacement_is_rejected_before_permissions_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'path'; path.mkdir(mode=0o755)
            other = Path(directory) / 'other'; other.mkdir(mode=0o755)
            real_open = os.open
            with patch.object(entrypoint.os, 'open', side_effect=lambda _path, flags: real_open(other, flags)):
                with self.assertRaises(OSError):
                    entrypoint.prepare_directory(str(path), tool_user=False)
            self.assertEqual(stat.S_IMODE(other.stat().st_mode), 0o755)

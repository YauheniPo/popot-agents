"""Coverage for the recovery and acceptance edges added by this branch."""

import base64
import unittest


def _check(**overrides):
    check = {'kind': 'checks', 'source': 'tool_result', 'status': 'passed', 'exitCode': 0,
             'summary': 'pytest', 'revision': None, 'commandSha256': 'a' * 64,
             'outputSha256': 'b' * 64}
    check.update(overrides)
    return check


class BudgetRecoveryTests(unittest.TestCase):
    def test_unstructured_previous_outcome_grants_recovery(self):
        from popot_agents.child_results import budget_recovery_allowed
        failure = {'resumable': True,
                   'outcome': {'reason': 'workflow_turn_limit', 'checks': [_check()]},
                   'previous_failure': {'outcome': ['not-a-dict']}}
        self.assertTrue(budget_recovery_allowed(failure, 1, 2))

    def test_recovery_requires_new_check_receipts(self):
        from popot_agents.child_results import budget_recovery_allowed
        failure = {'resumable': True,
                   'outcome': {'reason': 'workflow_turn_limit', 'checks': [_check()]},
                   'previous_failure': {'outcome': {'checks': [_check()]}}}
        self.assertFalse(budget_recovery_allowed(failure, 1, 2))


class WaitForChatProbeBudgetTests(unittest.TestCase):
    def test_probe_budget_exhaustion_reports_the_worker_as_not_ready(self):
        from types import SimpleNamespace
        from ax_local.api import ax_runner
        runner = ax_runner.AxAgentRunner.__new__(ax_runner.AxAgentRunner)
        runner.snapshot_mode = 'none'
        runner.task_status = lambda name, timeout: ('Running', None)
        calls = []

        def remaining(seconds):
            calls.append(seconds)
            if len(calls) >= 3:
                raise TimeoutError
            return 1

        chat = SimpleNamespace(container_name='popot-chat-' + 'a' * 16, probe_issue=None,
                               is_alive=lambda timeout_seconds: False, close=lambda: None)
        with self.assertRaises(ax_runner.AgentRunError) as raised:
            runner._wait_for_chat(chat, remaining, None)
        self.assertIn('did not become ready', str(raised.exception))


class StreamedProgressTests(unittest.TestCase):
    def test_non_progress_frames_are_not_forwarded(self):
        from types import SimpleNamespace
        from popot_agents.orchestrator.main import create_server
        server = create_server({})
        self.addCleanup(server.server_close)
        handler = SimpleNamespace(_wants_progress=True, _progress_scope={})
        self.assertIsNone(server.RequestHandlerClass._progress(handler, 'plain worker output'))


class AcceptanceRevisionMismatchTests(unittest.TestCase):
    def test_child_revision_mismatch_forces_partial(self):
        import json
        from tests import test_agent_reliability as reliability
        helper = reliability.AgentReliabilityTests()
        child = {'sessionId': 'a' * 16, 'role': 'backend_engineer',
                 'outcome': {'status': 'completed', 'revision': 'a' * 40,
                             'evidence': ['pushed the fix']}}
        payload = json.loads(helper.result(answer='done')['content'])
        payload['revision'] = 'b' * 40
        _, state, _ = helper.run_sequence(
            [{'content': json.dumps(payload)}],
            state={'messages': [{'role': 'user',
                                 'content': 'Fix https://github.com/example/repo/pull/47'}],
                   'outcome': {'status': 'partial'},
                   'completed_delegations': {'a' * 16: child}},
            role={'name': 'backend_engineer', 'tools': []},
            run_context={'session_id': 'a' * 16,
                         'authorization': ['Fix https://github.com/example/repo/pull/47']})
        self.assertEqual(state['outcome']['status'], 'partial')
        self.assertEqual(state['outcome']['reason'], 'acceptance_revision_mismatch')

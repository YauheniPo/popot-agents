"""Delegated sessions may only continue through delegate_task, never a raw chat id."""

import unittest
from types import SimpleNamespace

from popot_agents.orchestrator.main import AgentRunError

from ax_local.api.ax_runner import AxAgentRunner


def runner_with_jobs(jobs):
    runner = object.__new__(AxAgentRunner)
    runner.delegation = SimpleNamespace(jobs=jobs)
    return runner


class RejectUngrantedDelegationTests(unittest.TestCase):
    def test_unknown_chat_id_is_refused(self):
        runner = runner_with_jobs({'granted'})
        with self.assertRaisesRegex(AgentRunError, 'delegate_task'):
            runner._reject_ungranted_delegation('stranger', {'_ax_delegation': True})

    def test_granted_chat_id_is_allowed(self):
        runner = runner_with_jobs({'granted'})
        runner._reject_ungranted_delegation('granted', {'_ax_delegation': True})

    def test_plain_session_without_delegation_is_allowed(self):
        runner = runner_with_jobs(set())
        runner._reject_ungranted_delegation('plain', None)
        runner._reject_ungranted_delegation('plain', {})

    def test_runner_without_delegation_support_is_allowed(self):
        runner = object.__new__(AxAgentRunner)
        runner.delegation = None
        runner._reject_ungranted_delegation('plain', {'_ax_delegation': True})


if __name__ == '__main__':
    unittest.main()

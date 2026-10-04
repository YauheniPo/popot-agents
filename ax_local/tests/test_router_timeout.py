"""Local AX router must allow a model turn to finish before timing out."""

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock

from ax_local.cluster.router_timeout import ensure_router_timeout


class RouterTimeoutTests(unittest.TestCase):
    def test_updates_only_local_router_and_waits_for_rollout(self) -> None:
        deployment = {"spec": {"template": {"spec": {"containers": [
            {"name": "envoy", "args": ["envoy"]},
            {"name": "atenet-router", "args": ["router", "--route-timeout=10s"]},
        ]}}}}
        run = Mock(side_effect=[
            subprocess.CompletedProcess([], 0, json.dumps(deployment), ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ])
        ensure_router_timeout(Path("/local/kubeconfig"), "kind-popot-ax", 330, run=run)

        self.assertEqual(run.call_count, 3)
        for call in run.call_args_list:
            command = call.args[0]
            self.assertIn("--kubeconfig=/local/kubeconfig", command)
            self.assertIn("--context=kind-popot-ax", command)
            self.assertIn("-n", command)
            self.assertIn("ate-system", command)
        patch_command = run.call_args_list[1].args[0]
        patch_data = json.loads(patch_command[patch_command.index("-p") + 1])
        self.assertEqual(patch_data, [{
            "op": "replace",
            "path": "/spec/template/spec/containers/1/args",
            "value": ["router", "--route-timeout=330s"],
        }])
        self.assertIn("rollout", run.call_args_list[2].args[0])

    def test_does_not_redeploy_when_already_configured(self) -> None:
        deployment = {"spec": {"template": {"spec": {"containers": [
            {"name": "atenet-router", "args": ["router", "--route-timeout=330s"]},
        ]}}}}
        run = Mock(return_value=subprocess.CompletedProcess(
            [], 0, json.dumps(deployment), ""))
        ensure_router_timeout(Path("/local/kubeconfig"), "kind-popot-ax", 330, run=run)
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ax_local.worker.entrypoint import main


class EntrypointTests(unittest.TestCase):
    def test_root_overlay_allows_tool_uid_to_traverse_before_harness_starts(self):
        modes = {"/": 0o700, "/workspace": 0o755}

        def start_harness():
            # UID 10001 must cross / to execute Python or read its stdlib.
            self.assertEqual(modes["/"], 0o711)
            self.assertEqual(modes["/"] & 0o022, 0)
            self.assertEqual(modes["/workspace"], 0o1777)

        with patch("ax_local.worker.entrypoint.os.makedirs"), \
             patch("ax_local.worker.entrypoint.os.geteuid", return_value=0), \
             patch("ax_local.worker.entrypoint.os.stat",
                   side_effect=lambda path: SimpleNamespace(st_mode=modes[path])), \
             patch("ax_local.worker.entrypoint.os.chmod",
                   side_effect=lambda path, mode: modes.update({path: mode})), \
             patch("ax_local.worker.entrypoint.make_tools", return_value={}), \
             patch("ax_local.worker.entrypoint.serve", side_effect=start_harness):
            main()

    def test_ax_entrypoint_supplies_the_delegation_tool_to_session_harness(self):
        tools = {"delegate_task": {"schema": {}, "call": lambda *_: ""}}
        with patch("ax_local.worker.entrypoint.os.makedirs"), \
             patch("ax_local.worker.entrypoint.os.geteuid", return_value=10001), \
             patch("ax_local.worker.entrypoint.make_tools", return_value=tools), \
             patch("ax_local.worker.entrypoint.serve") as serve:
            main()
        serve.assert_called_once_with(extra_tools=tools)

    def test_workspace_setup_does_not_require_chown_capability(self):
        with patch("ax_local.worker.entrypoint.os.makedirs"), \
             patch("ax_local.worker.entrypoint.os.geteuid", return_value=0), \
             patch("ax_local.worker.entrypoint.os.stat",
                   return_value=SimpleNamespace(st_mode=0o755)), \
             patch("ax_local.worker.entrypoint.os.chown",
                   side_effect=PermissionError("Operation not permitted")), \
             patch("ax_local.worker.entrypoint.os.chmod") as chmod, \
             patch("ax_local.worker.entrypoint.serve") as serve:
            main()
        chmod.assert_called_once_with("/workspace", 0o1777)
        serve.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

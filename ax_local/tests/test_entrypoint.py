import unittest
import os
import json
import socket
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ax_local.worker.entrypoint import main
from ax_local.worker import entrypoint


class EntrypointTests(unittest.TestCase):
    def test_capability_bundle_cannot_override_unrelated_environment(self):
        for value in ('[]', '{"HOME":"/tmp"}', '{"AX_DELEGATION_TOKEN":123}'):
            with self.subTest(value=value), patch.dict(os.environ,
                    {'AX_CAPABILITIES_JSON': value, 'HOME': '/workspace'}, clear=True):
                with self.assertRaises(ValueError):
                    entrypoint.load_capabilities()
                self.assertEqual(os.environ['HOME'], '/workspace')

    def test_data_restart_removes_only_a_stale_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'chat.sock')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.bind(path)
            entrypoint.prepare_socket(path)
            self.assertFalse(os.path.lexists(path))
            entrypoint.prepare_socket(path)  # Fresh actor has no socket yet.

    def test_data_restart_refuses_to_replace_active_socket_files_or_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'chat.sock'
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.bind(str(path)); sock.listen(1)
                with self.assertRaisesRegex(RuntimeError, 'in use'):
                    entrypoint.prepare_socket(str(path))
                self.assertTrue(path.exists())
            path.unlink()
            path.write_text('keep this file')
            with self.assertRaisesRegex(RuntimeError, 'socket'):
                entrypoint.prepare_socket(str(path))
            self.assertEqual(path.read_text(), 'keep this file')
            target = Path(directory) / 'target'
            path.rename(target); path.symlink_to(target)
            with self.assertRaisesRegex(RuntimeError, 'socket'):
                entrypoint.prepare_socket(str(path))
            self.assertTrue(path.is_symlink())
            self.assertEqual(target.read_text(), 'keep this file')

    def test_root_overlay_allows_tool_uid_to_traverse_before_harness_starts(self):
        modes = {"/": 0o700, "/workspace": 0o1777, "/dev/shm": 0o755}
        owner = [10001, 10001]

        def chmod_workspace(fd, mode):
            modes["/workspace"] = mode

        def start_harness():
            # UID 10001 must cross / to execute Python or read its stdlib.
            self.assertEqual(modes["/"], 0o711)
            self.assertEqual(modes["/"] & 0o022, 0)
            self.assertEqual(modes["/workspace"], 0o700)
            self.assertEqual(owner, [10001, 10001])
            self.assertEqual(modes["/dev/shm"], 0o1777)

        with patch.dict(os.environ, {"POPOT_TOOLS_UID": "10001"}), \
             patch("ax_local.worker.entrypoint.os.makedirs"), \
             patch("ax_local.worker.entrypoint.os.open", return_value=99), \
             patch("ax_local.worker.entrypoint.os.close"), \
             patch("ax_local.worker.entrypoint.os.geteuid", return_value=0), \
             patch("ax_local.worker.entrypoint.os.stat",
                   side_effect=lambda path: SimpleNamespace(st_mode=modes[path])), \
             patch("ax_local.worker.entrypoint.os.chmod",
                   side_effect=lambda path, mode: modes.update({path: mode})), \
             patch("ax_local.worker.entrypoint.os.fchown",
                   side_effect=lambda fd, uid, gid: owner.__setitem__(slice(None), [uid, gid])), \
             patch("ax_local.worker.entrypoint.os.fchmod", side_effect=chmod_workspace), \
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

    def test_workspace_setup_fails_closed_without_required_capabilities(self):
        with patch.dict(os.environ, {"POPOT_TOOLS_UID": "10001"}), \
             patch.object(entrypoint.os, "makedirs"), \
             patch.object(entrypoint.os, "geteuid", return_value=0), \
             patch.object(entrypoint.os, "stat", return_value=SimpleNamespace(st_mode=0o755)), \
             patch.object(entrypoint.os, "open", return_value=99), \
             patch.object(entrypoint.os, "close") as close, \
             patch.object(entrypoint.os, "fchmod"), \
             patch.object(entrypoint.os, "fchown", side_effect=PermissionError("Operation not permitted")), \
             patch.object(entrypoint.os, "chmod"), \
             patch.object(entrypoint, "serve") as serve:
            with self.assertRaises(PermissionError):
                main()
        close.assert_called_once_with(99)
        serve.assert_not_called()

    def test_role_without_local_tools_keeps_private_root_workspace(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(entrypoint.os, "makedirs"), \
             patch.object(entrypoint.os, "geteuid", return_value=0), \
             patch.object(entrypoint.os, "stat", return_value=SimpleNamespace(st_mode=0o755)), \
             patch.object(entrypoint.os, "open", return_value=99), \
             patch.object(entrypoint.os, "close"), \
             patch.object(entrypoint.os, "fchown") as chown, \
             patch.object(entrypoint.os, "fchmod") as chmod, \
             patch.object(entrypoint.os, "chmod"), \
             patch.object(entrypoint, "prepare_socket"), \
             patch.object(entrypoint, "make_tools", return_value={}), \
             patch.object(entrypoint, "serve"):
            main()
        chown.assert_not_called()
        chmod.assert_called_once_with(99, 0o700)


if __name__ == "__main__":
    unittest.main()

"""Controller setup must stay within the isolated local kubeconfig."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ax_local.cluster.controller_tools import AX_COMMIT, VERSION


class ControllerSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.local = self.root / "ax_local/.local"
        self.log = self.root / "commands"
        scripts = self.root / "ax_local/cluster"
        scripts.mkdir(parents=True)
        source = Path(__file__).resolve().parents[1] / "cluster"
        for name in ("controller_tools.py", "controller_tools.sh"):
            shutil.copyfile(source / name, scripts / name)
        (self.root / "ax_local/config.json").write_text(
            '{"ax":{"context":"kind-popot-ax"}}', encoding="utf-8")
        client = self.local / "src/ax/internal/substrate/client.go"
        client.parent.mkdir(parents=True)
        client.write_text('// BuildActorTemplate constructs a Substrate ActorTemplate\n'
                          '\t\t\tName:    "guest",\n', encoding="utf-8")
        (self.local / "bin").mkdir(parents=True)
        (self.local / "kubeconfig").write_text("isolated local config", encoding="utf-8")
        self.company = self.root / "company-kubeconfig"
        self.company.write_text("company config must stay unchanged", encoding="utf-8")
        binary = self.root / "bin"
        binary.mkdir()
        (binary / "python3").symlink_to(sys.executable)
        commands = {
            "git": f"printf '%s\\n' '{AX_COMMIT}'",
            "gofmt": "exit 0",
            "go": "printf 'arm64\\n'",
            "kubectl": 'if [ "$1" = config ]; then printf "%s\\n" "$TEST_CONTEXT"; '
                       'else printf "%s|%s\\n" "$KUBECONFIG" "$*" >> "$TEST_LOG"; fi',
        }
        for name, body in commands.items():
            path = binary / name
            path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
            path.chmod(0o700)
        ko = self.local / "bin/ko"
        ko.write_text('#!/bin/sh\nprintf "%s|ko %s\\n" "$KUBECONFIG" "$*" >> "$TEST_LOG"\n',
                      encoding="utf-8")
        ko.chmod(0o700)
        self.env = {"PATH": str(binary) + os.pathsep + os.defpath,
                    "KUBECONFIG": str(self.company), "TEST_LOG": str(self.log)}

    def run_setup(self, context):
        return subprocess.run(["/bin/bash", str(self.root / "ax_local/cluster/controller_tools.sh")],
                              env={**self.env, "TEST_CONTEXT": context},
                              capture_output=True, text=True, timeout=10)

    def test_setup_uses_only_local_kubeconfig_and_skips_current_patch(self):
        first = self.run_setup("kind-popot-ax")
        self.assertEqual(first.returncode, 0, first.stderr)
        commands = self.log.read_text(encoding="utf-8")
        self.assertIn("ko apply -f deploy/ax-controller.yaml", commands)
        self.assertTrue(all(line.startswith(str(self.local / "kubeconfig") + "|")
                            for line in commands.splitlines()))
        self.assertIn("--context kind-popot-ax", commands)
        self.assertEqual((self.local / "controller-tools-version").read_text().strip(), VERSION)
        second = self.run_setup("kind-popot-ax")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.log.read_text(), commands)
        self.assertEqual(self.company.read_text(), "company config must stay unchanged")

    def test_wrong_context_prevents_controller_changes(self):
        result = self.run_setup("company")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing", result.stderr)
        self.assertFalse(self.log.exists())
        self.assertFalse((self.local / "controller-tools-version").exists())
        self.assertEqual(self.company.read_text(), "company config must stay unchanged")


if __name__ == "__main__":
    unittest.main()

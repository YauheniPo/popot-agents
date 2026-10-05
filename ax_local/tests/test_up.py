"""The AX one-command launcher selects setup or refresh safely."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SOURCE = Path(__file__).parents[1] / "up.sh"


class UpScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.ax = self.root / "ax_local"
        self.ax.mkdir()
        shutil.copyfile(SOURCE, self.ax / "up.sh")
        cluster = self.ax / "cluster"
        cluster.mkdir()
        shutil.copyfile(SOURCE.parent / "cluster" / "router_timeout.py",
                        cluster / "router_timeout.py")
        (self.ax / "config.json").write_text(
            json.dumps({"ax": {"context": "kind-popot-ax",
                               "route_timeout_seconds": 330}}), encoding="utf-8")
        (self.root / ".env").write_text("POPOT_DB_PASSWORD=test\n", encoding="utf-8")
        self.calls = self.root / "calls"
        for name in ("bootstrap.sh", "rebuild-worker.sh"):
            (self.ax / name).write_text(
                f"#!/usr/bin/env bash\nprintf '{name}\\n' >> \"$CALLS\"\n",
                encoding="utf-8",
            )
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        docker = fake_bin / "docker"
        docker.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'docker %s\\n' \"$*\" >> \"$CALLS\"\n"
            "if [[ $1 == inspect ]]; then\n"
            "  if [[ ${DOCKER_STOPPED:-} == kind-registry && $4 == kind-registry ]]; then\n"
            "    printf 'false\\n'\n"
            "  else\n"
            "    printf 'true\\n'\n"
            "  fi\n"
            "fi\n",
            encoding="utf-8",
        )
        docker.chmod(0o755)
        kubectl = fake_bin / "kubectl"
        kubectl.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'kubectl %s\\n' \"$*\" >> \"$CALLS\"\n"
            "if [[ \" $* \" == *' get deployment atenet-router -o json'* ]]; then\n"
            "  printf '%s\\n' '{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"atenet-router\",\"args\":[\"router\"]}]}}}}'\n"
            "fi\n",
            encoding="utf-8",
        )
        kubectl.chmod(0o755)
        self.env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "CALLS": str(self.calls)}

    def run_up(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(self.ax / "up.sh")], env=self.env,
                              text=True, capture_output=True, check=False)

    def prepare_local(self) -> None:
        local = self.ax / ".local"
        (local / "bin").mkdir(parents=True)
        (local / "src/ax/.git").mkdir(parents=True)
        for name in ("kubeconfig", "kubeconfig-container", "worker-image"):
            (local / name).write_text("ready\n", encoding="utf-8")
        for name in ("ax", "ko"):
            binary = local / "bin" / name
            binary.write_text("ready\n", encoding="utf-8")
            binary.chmod(0o755)

    def test_first_run_bootstraps_then_starts_compose(self) -> None:
        result = self.run_up()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text(encoding="utf-8").splitlines()
        self.assertEqual(calls[0], "bootstrap.sh")
        self.assertIn("compose --env-file", calls[-1])
        self.assertIn("up --build -d mcp-server", calls[-1])
        self.assertTrue(any("--kubeconfig=" in call and "--context=kind-popot-ax" in call
                            for call in calls if call.startswith("kubectl ")))

    def test_repeat_run_refreshes_worker_and_compose(self) -> None:
        self.prepare_local()
        result = self.run_up()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text(encoding="utf-8").splitlines()
        self.assertEqual(calls[-1], "rebuild-worker.sh")
        self.assertNotIn("bootstrap.sh", calls)

    def test_repeat_run_starts_stopped_local_registry(self) -> None:
        self.prepare_local()
        self.env["DOCKER_STOPPED"] = "kind-registry"
        result = self.run_up()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text(encoding="utf-8").splitlines()
        self.assertIn("docker start kind-registry", calls)
        self.assertEqual(calls[-1], "rebuild-worker.sh")


if __name__ == "__main__":
    unittest.main()

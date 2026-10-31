"""The AX one-command launcher selects setup or refresh safely."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SOURCE = Path(__file__).parents[1] / "up.sh"


class ComposeProvisioningTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('docker'), 'Docker Compose CLI is required')
    def test_gateway_health_and_successful_provision_are_required_before_api(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / 'project'
            ax = project / 'ax_local'
            ax.mkdir(parents=True)
            for name in ('compose.yaml', 'compose.omniroute.yaml'):
                shutil.copyfile(SOURCE.parent / name, ax / name)
            env_file = project / '.env'
            env_file.write_text('POPOT_DB_PASSWORD=compose-test\n')
            result = subprocess.run([
                'bash', str(SOURCE.parent.parent / 'compose.sh'), '--env-file', str(env_file),
                '-f', str(ax / 'compose.yaml'),
                '-f', str(ax / 'compose.omniroute.yaml'),
                '--profile', 'omniroute', 'config', '--format', 'json'],
                cwd=temporary,
                env={key: value for key, value in os.environ.items()
                     if not key.startswith('COMPOSE_')},
                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        services = json.loads(result.stdout)['services']
        gate = services['api']['depends_on']['omniroute-init']
        self.assertEqual(gate, {'condition': 'service_completed_successfully', 'required': True})
        self.assertEqual(services['omniroute-init']['depends_on']['omniroute']['condition'],
                         'service_healthy')
        self.assertTrue(all(mount['read_only'] for mount in services['omniroute-init']['volumes']))
        self.assertNotIn('env_file', services['omniroute-init'])
        self.assertEqual(services['omniroute']['depends_on']['omniroute-tls-init']['condition'],
                         'service_completed_successfully')
        self.assertEqual(services['omniroute-init']['environment']['OMNIROUTE_URL'], 'https://omniroute:20128')
        self.assertEqual(services['omniroute']['environment']['OMNIROUTE_TLS_KEY'], '/run/popot-tls/key.pem')
        for name in ('api', 'omniroute-init'):
            trust = next(mount for mount in services[name]['volumes'] if mount['target'] == '/run/popot-tls')
            self.assertTrue(trust['read_only'])
            self.assertEqual(trust['volume']['subpath'], 'trust')
            self.assertEqual(services[name]['environment']['SSL_CERT_FILE'], '/run/popot-tls/ca-bundle.pem')
        self.assertEqual(services['omniroute']['ports'][0]['host_ip'], '127.0.0.1')


class UpScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.ax = self.root / "ax_local"
        self.ax.mkdir()
        memory = self.root / 'memory'
        memory.mkdir()
        (memory / 'up.sh').write_text(
            '#!/usr/bin/env bash\nprintf "memory-up\\n" >> "$CALLS"\n')
        shutil.copyfile(SOURCE, self.ax / "up.sh")
        shutil.copyfile(SOURCE.parent / 'compose.sh', self.ax / 'compose.sh')
        config = self.root / 'config'
        config.mkdir()
        (config / 'roles').mkdir()
        (config / 'roles/analyst.json').write_text(json.dumps({'agent': 'omniroute'}))
        cluster = self.ax / "cluster"
        cluster.mkdir()
        shutil.copyfile(SOURCE.parent / 'cluster/build_versions.py', cluster / 'build_versions.py')
        shutil.copyfile(SOURCE.parent.parent / 'pyproject.toml', self.root / 'pyproject.toml')
        shutil.copyfile(SOURCE.parent.parent / 'compose.sh', self.root / 'compose.sh')
        shutil.copyfile(SOURCE.parent / "cluster" / "router_timeout.py",
                        cluster / "router_timeout.py")
        (cluster / "worker_health.py").write_text(
            "import os\nfrom pathlib import Path\n"
            "with Path(os.environ['CALLS']).open('a') as f: f.write('worker-health\\n')\n"
            "raise SystemExit(int(os.environ.get('HEALTH_FAIL', '0')))\n")
        (cluster / "substrate_cleanup.py").write_text(
            "import os, sys\nfrom pathlib import Path\n"
            "assert sys.argv[1:] == ['--apply']\n"
            "with Path(os.environ['CALLS']).open('a') as f: f.write('substrate-cleanup\\n')\n"
            "raise SystemExit(int(os.environ.get('CLEANUP_FAIL', '0')))\n")
        (cluster / 'deployment_cleanup.py').write_text(
            "import os\nfrom pathlib import Path\n"
            "with Path(os.environ['CALLS']).open('a') as f: f.write('deployment-cleanup\\n')\n"
            "raise SystemExit(int(os.environ.get('DEPLOYMENT_CLEANUP_FAIL', '0')))\n")
        (cluster / 'resource_monitoring.py').write_text(
            "import os, sys\nfrom pathlib import Path\n"
            "assert sys.argv[1:] in (['install'], ['ensure-forward'])\n"
            "event = 'resource-monitoring' if sys.argv[1] == 'install' else 'grafana-forward-check'\n"
            "with Path(os.environ['CALLS']).open('a') as f: f.write(event + '\\n')\n"
            "raise SystemExit(int(os.environ.get('MONITORING_FAIL', '0')))\n")
        (self.root / "ax_local/config.json").write_text(
            json.dumps({"context": "kind-popot-ax", "route_timeout_seconds": 330}), encoding="utf-8")
        (self.root / ".env").write_text("POPOT_DB_PASSWORD=test\n", encoding="utf-8")
        self.calls = self.root / "calls"
        for name in ("bootstrap.sh", "rebuild-worker.sh"):
            (self.ax / name).write_text(
                f"#!/usr/bin/env bash\nprintf '{name}\\n' >> \"$CALLS\"\n",
                encoding="utf-8",
            )
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        (fake_bin / 'python3').symlink_to(sys.executable)
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
        (local / "src/substrate/.git").mkdir(parents=True)
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
        self.assertIn("substrate-cleanup", calls)
        self.assertLess(calls.index("substrate-cleanup"), calls.index("worker-health"))
        self.assertLess(calls.index('worker-health'), calls.index('memory-up'))
        self.assertEqual(calls[-4], 'memory-up')
        self.assertIn("compose --env-file", calls[-3])
        self.assertIn("up --build -d --wait --wait-timeout 300 mcp-server", calls[-3])
        self.assertEqual(calls[-2:], ['deployment-cleanup', 'resource-monitoring'])
        self.assertTrue(any("--kubeconfig=" in call and "--context=kind-popot-ax" in call
                            for call in calls if call.startswith("kubectl ")))

    def test_invalid_build_config_prevents_cluster_changes(self):
        (self.root / 'pyproject.toml').write_text('[tool.popot.build]\n')
        result = self.run_up()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.calls.exists())

    def test_operator_prepares_holmes_without_starting_an_investigation(self):
        (self.root / 'config/roles/project_operator.json').write_text(json.dumps({'agent':'omniroute'}))
        (self.ax / 'omniroute_tls.py').write_text(
            "import os,sys\nfrom pathlib import Path\nassert sys.argv[1:]==['export']\n"
            "with Path(os.environ['CALLS']).open('a') as f:f.write('tls-export\\n')\n")
        (self.ax / 'cluster/holmes.py').write_text(
            "import os,sys\nfrom pathlib import Path\nassert sys.argv[1:]==['--prepare']\n"
            "with Path(os.environ['CALLS']).open('a') as f:f.write('holmes-prepare\\n')\n")
        result=self.run_up()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.calls.read_text().splitlines()[-2:],['tls-export','holmes-prepare'])

    def test_omniroute_roles_select_provisioning_dependency(self):
        result = self.run_up()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text()
        self.assertIn('compose.omniroute.yaml --profile omniroute', calls)

    def test_other_profiles_do_not_start_omniroute(self):
        (self.root / 'config/roles/analyst.json').write_text(
            json.dumps({'agent': 'openrouter'}))
        result = self.run_up()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('compose.omniroute.yaml', self.calls.read_text())

    def test_compose_wrapper_selects_omniroute_provisioning(self):
        self.prepare_local()
        result = subprocess.run(['bash', str(self.ax / 'compose.sh'), 'config'],
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('compose.omniroute.yaml --profile omniroute config', self.calls.read_text())

    def test_failed_worker_recovery_prevents_api_start(self):
        self.env["HEALTH_FAIL"] = "1"
        result = self.run_up()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("compose", self.calls.read_text())

    def test_repeat_run_refreshes_worker_and_compose(self) -> None:
        self.prepare_local()
        result = self.run_up()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text(encoding="utf-8").splitlines()
        self.assertEqual(calls[-3:], ["rebuild-worker.sh", 'deployment-cleanup', 'resource-monitoring'])
        self.assertIn("substrate-cleanup", calls)
        self.assertNotIn("bootstrap.sh", calls)

    def test_cleanup_failure_prevents_api_start_on_fresh_and_repeat_run(self):
        self.env["CLEANUP_FAIL"] = "1"
        for prepared in (False, True):
            with self.subTest(prepared=prepared):
                if prepared:
                    self.prepare_local()
                    self.calls.write_text("")
                result = self.run_up()
                self.assertNotEqual(result.returncode, 0)
                calls = self.calls.read_text()
                self.assertIn("substrate-cleanup", calls)
                self.assertNotIn("compose", calls)
                self.assertNotIn("rebuild-worker.sh", calls)

    def test_repeat_run_starts_stopped_local_registry(self) -> None:
        self.prepare_local()
        self.env["DOCKER_STOPPED"] = "kind-registry"
        result = self.run_up()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text(encoding="utf-8").splitlines()
        self.assertIn("docker start kind-registry", calls)
        self.assertEqual(calls[-3:], ["rebuild-worker.sh", 'deployment-cleanup', 'resource-monitoring'])

    def test_rebuild_checks_workers_before_starting_api(self):
        self.prepare_local()
        shutil.copyfile(SOURCE.parent / "rebuild-worker.sh", self.ax / "rebuild-worker.sh")
        (self.ax / "cluster/controller_tools.sh").write_text("exit 0\n")
        fake_bin = self.root / "bin"
        go = fake_bin / "go"
        go.write_text("#!/usr/bin/env bash\necho arm64\n")
        go.chmod(0o755)
        with (fake_bin / "docker").open("a") as f:
            f.write('if [[ $1 == image ]]; then echo "localhost:5001/popot-agent-ax@sha256:'
                    + 'a' * 64 + '"; fi\n')
        result = self.run_up()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text().splitlines()
        self.assertIn("worker-health", calls)
        self.assertIn("up --build --force-recreate -d --wait --wait-timeout 300 api mcp-server", calls[-4])
        self.assertIn('compose.omniroute.yaml --profile omniroute', calls[-4])
        self.assertEqual(calls[-3:], ['grafana-forward-check', 'deployment-cleanup', 'resource-monitoring'])
        self.calls.write_text("")
        self.env["MONITORING_FAIL"] = "1"
        result = subprocess.run(["bash", str(self.ax / "rebuild-worker.sh")], env=self.env,
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls.read_text().splitlines()[-1], 'grafana-forward-check')
        self.env.pop("MONITORING_FAIL")
        self.calls.write_text("")
        self.env["HEALTH_FAIL"] = "1"
        result = subprocess.run(["bash", str(self.ax / "rebuild-worker.sh")], env=self.env,
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls.read_text().splitlines(), ["worker-health"])

    def test_cleanup_failure_is_visible_as_failed_up(self):
        self.prepare_local()
        self.env['DEPLOYMENT_CLEANUP_FAIL'] = '1'
        self.assertNotEqual(self.run_up().returncode, 0)

    def test_monitoring_failure_is_visible_as_failed_up(self):
        self.prepare_local()
        self.env['MONITORING_FAIL'] = '1'
        self.assertNotEqual(self.run_up().returncode, 0)


if __name__ == "__main__":
    unittest.main()

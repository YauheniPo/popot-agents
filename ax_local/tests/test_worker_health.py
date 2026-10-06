import json
import subprocess
import unittest
import tempfile
from unittest.mock import patch
from pathlib import Path

from ax_local.cluster import worker_health
from ax_local.cluster.worker_health import ensure_workers


def pod(name="worker-a", uid="uid-a", ip="10.0.0.2"):
    return {"metadata": {"name": name, "namespace": "ate-demo-counter", "uid": uid,
                         "resourceVersion": "12", "labels": {"ate.dev/worker-pool": "counter"},
                         "ownerReferences": [{"kind": "ReplicaSet", "controller": True}]},
            "status": {"podIP": ip, "conditions": [{"type": "Ready", "status": "True"}]}}


def worker(p, ip=None):
    return {"workerNamespace": p["metadata"]["namespace"], "workerPod": p["metadata"]["name"],
            "workerPodUid": p["metadata"]["uid"], "workerPool": "counter",
            "ip": ip or p["status"]["podIP"], "status": {"state": "WORKER_STATE_ACTIVE"}}


class WorkerHealthTests(unittest.TestCase):
    def run_health(self, snapshots, *, fail_read=False, fail_delete=False, context="kind-popot-ax", replicas=1):
        self.calls = []
        self.tick = 0
        self.index = 0

        def run(args, **kwargs):
            self.calls.append((args, kwargs))
            self.assertLessEqual(kwargs["timeout"], 10)
            if fail_read or (fail_delete and "delete" in args):
                raise subprocess.CalledProcessError(1, args, stderr="private details")
            pods, workers = snapshots[min(self.index, len(snapshots)-1)]
            if "delete" in args:
                return subprocess.CompletedProcess(args, 0, "{}")
            if "workerpools" in args:
                data = {"items": [{"metadata": {"name": "counter"}, "spec": {"replicas": replicas}}]}
            elif "pods" in args:
                data = {"items": pods}
            else:
                data = {"workers": workers}
                self.index += 1
            return subprocess.CompletedProcess(args, 0, json.dumps(data))

        def sleep(seconds):
            self.tick += seconds

        ensure_workers(Path("kubeconfig"), Path("kubectl-ate"), context,
                       "ate-demo-counter", 10, run=run,
                       clock=lambda: self.tick, sleep=sleep)

    def test_healthy_workers_are_not_restarted(self):
        p = pod()
        self.run_health([([p], [worker(p)])])
        self.assertFalse(any("delete" in args for args, _ in self.calls))

    def test_changed_ip_replaces_only_stale_pod_with_uid_precondition(self):
        old = pod()
        good = pod("worker-b", "uid-b", "10.0.0.3")
        new = pod("worker-new", "uid-new", "10.0.0.4")
        self.run_health([([old, good], [worker(old, "10.0.0.99"), worker(good)]),
                         ([new, good], [worker(new), worker(good)])], replicas=2)
        deletes = [(args, kw) for args, kw in self.calls if "delete" in args]
        self.assertEqual(len(deletes), 1)
        args, kw = deletes[0]
        self.assertIn("/api/v1/namespaces/ate-demo-counter/pods/worker-a", args)
        self.assertEqual(json.loads(kw["input"])["preconditions"],
                         {"uid": "uid-a", "resourceVersion": "12"})

    def test_waits_for_registration_without_restarting_new_pod(self):
        p = pod()
        self.run_health([([p], []), ([p], [worker(p)])])
        self.assertFalse(any("delete" in args for args, _ in self.calls))
        self.assertGreater(self.tick, 0)

    def test_waits_for_orphan_registry_record_to_disappear(self):
        p = pod()
        orphan = worker(pod("old", "old-uid"))
        self.run_health([([p], [worker(p), orphan]), ([p], [worker(p)])])
        self.assertGreater(self.tick, 0)
        self.assertFalse(any("delete" in args for args, _ in self.calls))

    def test_same_name_new_uid_is_not_deleted_for_old_registration(self):
        p = pod()
        old = worker(pod(uid="old-uid"), "10.0.0.99")
        self.run_health([([p], [old]), ([p], [worker(p)])])
        self.assertFalse(any("delete" in args for args, _ in self.calls))

    def test_not_ready_or_unmanaged_pods_are_never_deleted(self):
        for key in ("ready", "owner"):
            p = pod()
            if key == "ready":
                p["status"]["conditions"] = []
            else:
                p["metadata"]["ownerReferences"] = []
            with self.subTest(key=key), self.assertRaises((TimeoutError, ValueError)):
                self.run_health([([p], [worker(p, "10.0.0.99")])])
            self.assertFalse(any("delete" in args for args, _ in self.calls))

    def test_failed_inspection_cannot_report_healthy_or_delete(self):
        with self.assertRaises(TimeoutError):
            self.run_health([], fail_read=True)
        self.assertFalse(any("delete" in args for args, _ in self.calls))

    def test_delete_conflict_stops_recovery(self):
        p = pod()
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_health([([p], [worker(p, "10.0.0.99")])], fail_delete=True)
        self.assertEqual(sum("delete" in args for args, _ in self.calls), 1)

    def test_empty_cluster_times_out_instead_of_reporting_success(self):
        with self.assertRaises(TimeoutError):
            self.run_health([([], [])])
        self.assertEqual(self.tick, 10)

    def test_refuses_nonlocal_context_before_any_command(self):
        with self.assertRaises(ValueError):
            self.run_health([], context="production")
        self.assertEqual(self.calls, [])

    def test_pod_is_deleted_at_most_once_while_waiting_for_replacement(self):
        p = pod()
        with self.assertRaises(TimeoutError):
            self.run_health([([p], [worker(p, "10.0.0.99")])])
        self.assertEqual(sum("delete" in args for args, _ in self.calls), 1)

    def test_waits_for_all_desired_replicas_and_registrations(self):
        a = pod()
        b = pod("worker-b", "uid-b", "10.0.0.3")
        self.run_health([([a], [worker(a)]), ([a, b], [worker(a)]),
                         ([a, b], [worker(a), worker(b)])], replicas=2)
        self.assertEqual(self.index, 3)
        self.assertFalse(any("delete" in args for args, _ in self.calls))


class WorkerHealthMainTests(unittest.TestCase):
    def test_build_and_cache_use_ax_local_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            ax = Path(temp).resolve() / "ax_local"
            ax.mkdir()
            (ax / "config.json").write_text(json.dumps({"ax": {
                "context": "kind-popot-ax", "atespace": "ate-demo-counter",
                "startup_timeout_seconds": 300}}))
            cli = ax / ".local/bin/kubectl-ate"
            def run(args, **kw):
                self.assertEqual(kw["cwd"], ax / ".local/src/substrate")
                if args[0] == "go":
                    cli.write_text("compiled")
                return subprocess.CompletedProcess(args, 0, worker_health.SUBSTRATE_COMMIT)
            with patch.object(worker_health, "__file__", str(ax / "cluster/worker_health.py")), \
                 patch.object(worker_health.subprocess, "run", side_effect=run) as command, \
                 patch.object(worker_health, "ensure_workers") as ensure:
                worker_health.main()
                worker_health.main()
            self.assertEqual(sum(c.args[0][0] == "go" for c in command.call_args_list), 1)
            ensure.assert_called_with(ax / ".local/kubeconfig", cli, "kind-popot-ax",
                                      "ate-demo-counter", 300)

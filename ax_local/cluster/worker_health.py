"""Repair stale Substrate worker IP registrations before starting local AX API."""

from __future__ import annotations

import fcntl
import json
import re
import subprocess
import time
from pathlib import Path


SUBSTRATE_COMMIT = "672533541dbfcd29084e4de2475267088bda3651"
WORKER_LABEL = "ate.dev/worker-pool"


def ensure_workers(kubeconfig: Path, cli: Path, context: str, namespace: str,
                   timeout_seconds: int, *, run=subprocess.run,
                   clock=time.monotonic, sleep=time.sleep) -> None:
    if not re.fullmatch(r"kind-[a-z0-9-]+", context):
        raise ValueError("Worker recovery requires a local kind context")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", namespace):
        raise ValueError("Invalid worker namespace")
    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        raise ValueError("Worker recovery timeout must be positive")
    flags = [f"--kubeconfig={kubeconfig}", f"--context={context}"]
    kube = ["kubectl", *flags, "--request-timeout=15s"]
    deadline = clock() + timeout_seconds
    replaced = set()
    last_progress = None

    def progress(message):
        nonlocal last_progress
        if message != last_progress:
            print(message, flush=True)
            last_progress = message

    def execute(args, **kwargs):
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("Local worker recovery timed out; API startup stopped")
        return run(args, text=True, capture_output=True, check=True,
                   timeout=min(30, remaining), **kwargs)

    while clock() < deadline:
        try:
            pools = json.loads(execute([*kube, "-n", namespace, "get", "workerpools",
                                        "-o", "json"]).stdout)["items"]
            pods = json.loads(execute([*kube, "-n", namespace, "get", "pods",
                                       "-l", WORKER_LABEL, "-o", "json"]).stdout)["items"]
            registry = json.loads(execute([str(cli), *flags, "get", "workers",
                                           "-n", namespace, "-o", "json"]).stdout)
            workers = registry.get("workers", [])  # protobuf omits empty repeated fields
            if not all(isinstance(items, list) for items in (pools, pods, workers)):
                raise ValueError("Invalid worker inspection response")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            # Kubernetes/API may still be coming back after Docker has started.
            progress(f"Waiting for Kubernetes/Substrate worker inspection: {type(exc).__name__}")
            sleep(min(2, max(0, deadline - clock())))
            continue

        desired = {p["metadata"]["name"]: p["spec"]["replicas"] for p in pools}
        if any(type(count) is not int or count < 0 for count in desired.values()):
            raise ValueError("Invalid WorkerPool replica count")
        # A partial deployment must not pass merely because its first pod registered.
        counts = {name: sum(p["metadata"].get("labels", {}).get(WORKER_LABEL) == name
                            for p in pods) for name in desired}
        healthy = bool(pods) and counts == desired
        seen = set()
        for pod in pods:
            meta = pod["metadata"]
            uid, name = meta["uid"], meta["name"]
            if (meta.get("namespace") != namespace
                    or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", name)
                    or meta.get("labels", {}).get(WORKER_LABEL) not in desired):
                raise ValueError("Worker pod is outside the selected local pool")
            seen.add(uid)
            status = pod.get("status", {})
            ip = status.get("podIP")
            ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                        for c in status.get("conditions", []))
            if not ip or not ready or meta.get("deletionTimestamp") or uid in replaced:
                healthy = False
                continue
            matches = [w for w in workers if w.get("workerPodUid") == uid
                       and w.get("workerPod") == name
                       and w.get("workerNamespace") == namespace
                       and w.get("workerPool") == meta["labels"][WORKER_LABEL]]
            if len(matches) != 1:
                healthy = False
                continue
            worker = matches[0]
            if not worker.get("ip"):
                raise ValueError("Worker registry record has no IP")
            if worker.get("ip") != ip:
                if not any(owner.get("controller") is True and owner.get("kind") == "ReplicaSet"
                           for owner in meta.get("ownerReferences", [])):
                    raise ValueError("Stale worker has no ReplicaSet to recreate its pod")
                print(f"Replacing stale AX worker {namespace}/{name}: "
                      f"registered IP {worker.get('ip')}, pod IP {ip}", flush=True)
                # Both identity and resource version must still match our observation.
                # A concurrent rollout or IP change aborts deletion instead of hitting a new pod.
                options = {"apiVersion": "v1", "kind": "DeleteOptions",
                           "preconditions": {"uid": uid, "resourceVersion": meta["resourceVersion"]}}
                execute([*kube, "delete", "--raw",
                         f"/api/v1/namespaces/{namespace}/pods/{name}", "-f", "-"],
                        input=json.dumps(options))
                replaced.add(uid)
                healthy = False
            elif worker.get("status", {}).get("state") != "WORKER_STATE_ACTIVE":
                healthy = False
        # Do not enable traffic while the registry can still select an orphan worker.
        if any(w.get("workerPodUid") not in seen for w in workers):
            healthy = False
        if healthy:
            print(f"Local AX workers ready: {len(pods)}; repaired: {len(replaced)}", flush=True)
            return
        progress(f"Waiting for AX workers: desired={sum(desired.values())}, "
                 f"pods={len(pods)}, registered={len(workers)}, repaired={len(replaced)}")
        sleep(min(2, max(0, deadline - clock())))
    raise TimeoutError("Local worker recovery timed out; API startup stopped")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    local = root / ".local"
    config = json.loads((root / "config.json").read_text())["ax"]
    # Serialize concurrent up/rebuild invocations. No persistent success marker:
    # every launch must compare today's pod IPs against the actual registry.
    local.mkdir(exist_ok=True)
    with (local / "worker-health.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        source = local / "src/substrate"
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source,
                              capture_output=True, text=True, check=True, timeout=10).stdout.strip()
        if head != SUBSTRATE_COMMIT:
            raise ValueError("Worker inspection requires the pinned Substrate checkout")
        cli = local / "bin/kubectl-ate"
        stamp = local / "worker-health-cli-version"
        if not cli.is_file() or not stamp.is_file() or stamp.read_text().strip() != head:
            print("Building local Substrate worker inspection CLI", flush=True)
            cli.parent.mkdir(exist_ok=True)
            subprocess.run(["go", "build", "-mod=vendor", "-o", str(cli), "./cmd/kubectl-ate"],
                           cwd=source, check=True, timeout=300)
            stamp.write_text(head + "\n")
        ensure_workers(local / "kubeconfig", cli, config["context"], config["atespace"],
                       config["startup_timeout_seconds"])


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        detail = str(exc) if isinstance(exc, (ValueError, TimeoutError)) else type(exc).__name__
        raise SystemExit("AX worker recovery failed; API startup stopped: " + detail)

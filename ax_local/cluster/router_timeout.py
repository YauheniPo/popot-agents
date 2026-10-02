"""Set the workload route timeout on the isolated local AX router."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable


def ensure_router_timeout(kubeconfig: Path, context: str, timeout_seconds: int,
                          *, run: Callable = subprocess.run) -> None:
    if not re.fullmatch(r"kind-[a-z0-9-]+", context):
        raise ValueError("AX router changes require a local kind context")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600:
        raise ValueError("AX router timeout must be between 1 and 3600 seconds")
    base = ["kubectl", f"--kubeconfig={kubeconfig}", f"--context={context}",
            "-n", "ate-system"]
    result = run([*base, "get", "deployment", "atenet-router", "-o", "json"],
                 text=True, capture_output=True, check=True)
    deployment = json.loads(result.stdout)
    containers = deployment["spec"]["template"]["spec"]["containers"]
    matches = [(index, container) for index, container in enumerate(containers)
               if container.get("name") == "atenet-router"]
    if len(matches) != 1:
        raise ValueError("local AX router deployment has no unique atenet-router container")
    index, container = matches[0]
    args = container.get("args", [])
    desired = f"--route-timeout={timeout_seconds}s"
    if [arg for arg in args if arg.startswith("--route-timeout=")] == [desired]:
        return
    updated = [arg for arg in args if not arg.startswith("--route-timeout=")]
    updated.append(desired)
    patch = [{"op": "replace" if "args" in container else "add",
              "path": f"/spec/template/spec/containers/{index}/args", "value": updated}]
    run([*base, "patch", "deployment", "atenet-router", "--type=json",
         "-p", json.dumps(patch, separators=(",", ":"))],
        text=True, capture_output=True, check=True)
    run([*base, "rollout", "status", "deployment/atenet-router", "--timeout=300s"],
        text=True, capture_output=True, check=True)


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: router_timeout.py LOCAL_KUBECONFIG AX_CONFIG_JSON")
    kubeconfig = Path(sys.argv[1]).resolve()
    config = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    ax = config["ax"]
    ensure_router_timeout(kubeconfig, ax["context"], ax["route_timeout_seconds"])
    print(f"Local AX router timeout: {ax['route_timeout_seconds']}s", flush=True)


if __name__ == "__main__":
    main()

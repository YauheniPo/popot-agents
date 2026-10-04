"""Prepare the isolated kind kubeconfig for a container on the Docker bridge."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


CONTEXT = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text(encoding="utf-8"))["ax"]["context"]


def rewrite_for_container(config: dict) -> dict:
    result = copy.deepcopy(config)
    if result.get("current-context") != CONTEXT:
        raise ValueError(f"kubeconfig must select {CONTEXT}")
    contexts = [item for item in result.get("contexts", []) if item.get("name") == CONTEXT]
    if len(contexts) != 1:
        raise ValueError("kind context is missing from kubeconfig")
    cluster_name = contexts[0]["context"]["cluster"]
    clusters = [item for item in result.get("clusters", []) if item.get("name") == cluster_name]
    if len(clusters) != 1:
        raise ValueError("kind cluster is missing from kubeconfig")
    cluster = clusters[0]["cluster"]
    endpoint = urlsplit(cluster["server"])
    if endpoint.scheme != "https" or endpoint.hostname not in {"127.0.0.1", "localhost", "::1"} \
            or not endpoint.port or endpoint.path not in {"", "/"}:
        raise ValueError("kind API endpoint must be a local HTTPS port")
    cluster["server"] = f"https://{CONTEXT.removeprefix('kind-')}-control-plane:6443"
    # The kind API certificate is issued for the original loopback name.
    cluster["tls-server-name"] = endpoint.hostname
    return result


def main(args: list[str]) -> None:
    if len(args) != 2:
        raise SystemExit("usage: container_kubeconfig.py SOURCE OUTPUT")
    source, target = Path(args[0]), Path(args[1])
    view = subprocess.run(
        ["kubectl", f"--kubeconfig={source}", "config", "view", "--raw", "--minify", "-o", "json"],
        text=True, capture_output=True, check=False,
    )
    if view.returncode:
        raise SystemExit("kubectl could not export the isolated kind kubeconfig")
    config = rewrite_for_container(json.loads(view.stdout))
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent,
                                         prefix=".kubeconfig-", delete=False) as file:
            temporary = Path(file.name)
            json.dump(config, file)
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main(sys.argv[1:])

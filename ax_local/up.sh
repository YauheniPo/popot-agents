#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCAL="$ROOT/ax_local/.local"
python3 "$ROOT/ax_local/cluster/build_versions.py" >/dev/null

if [[ ! -f "$ROOT/.env" ]]; then
  echo "Missing $ROOT/.env; configure the database password and provider credentials first" >&2
  exit 1
fi

if [[ ! -s "$LOCAL/kubeconfig" || ! -s "$LOCAL/kubeconfig-container" || \
      ! -s "$LOCAL/worker-image" || ! -x "$LOCAL/bin/ax" || \
      ! -d "$LOCAL/src/ax/.git" || ! -d "$LOCAL/src/substrate/.git" || ! -x "$LOCAL/bin/ko" ]]; then
  bash "$ROOT/ax_local/bootstrap.sh"
  python3 "$ROOT/ax_local/cluster/substrate_cleanup.py" --apply
  python3 "$ROOT/ax_local/cluster/router_timeout.py" \
    "$LOCAL/kubeconfig" "$ROOT/ax_local/config.json"
  python3 "$ROOT/ax_local/cluster/worker_health.py"
  bash "$ROOT/memory/up.sh"
  bash "$ROOT/ax_local/compose.sh" up --build -d --wait --wait-timeout 300 mcp-server
else
  CLUSTER="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["context"].removeprefix("kind-"))' "$ROOT/ax_local/config.json")"
  for container in kind-registry "${CLUSTER}-control-plane"; do
    if ! docker inspect "$container" >/dev/null 2>&1; then
      echo "Local AX container $container is missing; inspect ax_local/.local before restarting" >&2
      exit 1
    fi
    if [[ "$(docker inspect -f '{{.State.Running}}' "$container")" != "true" ]]; then
      docker start "$container" >/dev/null
    fi
  done
  python3 "$ROOT/ax_local/cluster/substrate_cleanup.py" --apply
  python3 "$ROOT/ax_local/cluster/router_timeout.py" \
    "$LOCAL/kubeconfig" "$ROOT/ax_local/config.json"
  bash "$ROOT/memory/up.sh"
  bash "$ROOT/ax_local/rebuild-worker.sh"
fi

python3 "$ROOT/ax_local/cluster/deployment_cleanup.py"
python3 "$ROOT/ax_local/cluster/resource_monitoring.py" install
if [[ -f "$ROOT/config/roles/project_operator.json" ]]; then
  python3 "$ROOT/ax_local/omniroute_tls.py" export
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 "$ROOT/ax_local/cluster/holmes.py" --prepare
fi

echo "AX MCP endpoint: http://127.0.0.1:${AX_MCP_PUBLISH_PORT:-8003}/mcp"

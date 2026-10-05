#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCAL="$ROOT/ax_local/.local"

if [[ ! -f "$ROOT/.env" ]]; then
  echo "Missing $ROOT/.env; configure the database password and provider credentials first" >&2
  exit 1
fi

if [[ ! -s "$LOCAL/kubeconfig" || ! -s "$LOCAL/kubeconfig-container" || \
      ! -s "$LOCAL/worker-image" || ! -x "$LOCAL/bin/ax" || \
      ! -d "$LOCAL/src/ax/.git" || ! -x "$LOCAL/bin/ko" ]]; then
  bash "$ROOT/ax_local/bootstrap.sh"
  python3 "$ROOT/ax_local/cluster/router_timeout.py" \
    "$LOCAL/kubeconfig" "$ROOT/ax_local/config.json"
  docker compose --env-file "$ROOT/.env" -f "$ROOT/ax_local/compose.yaml" \
    up --build -d mcp-server
else
  CLUSTER="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["ax"]["context"].removeprefix("kind-"))' "$ROOT/ax_local/config.json")"
  for container in kind-registry "${CLUSTER}-control-plane"; do
    if ! docker inspect "$container" >/dev/null 2>&1; then
      echo "Local AX container $container is missing; inspect ax_local/.local before restarting" >&2
      exit 1
    fi
    if [[ "$(docker inspect -f '{{.State.Running}}' "$container")" != "true" ]]; then
      docker start "$container" >/dev/null
    fi
  done
  python3 "$ROOT/ax_local/cluster/router_timeout.py" \
    "$LOCAL/kubeconfig" "$ROOT/ax_local/config.json"
  bash "$ROOT/ax_local/rebuild-worker.sh"
fi

echo "AX MCP endpoint: http://127.0.0.1:${AX_MCP_PUBLISH_PORT:-8003}/mcp"

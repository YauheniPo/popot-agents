#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LOCAL="$ROOT/ax_local/.local"
PATCHER="$ROOT/ax_local/cluster/controller_tools.py"
STAMP="$LOCAL/controller-tools-version"

if python3 "$PATCHER" "$LOCAL/src/ax" --check "$STAMP"; then
  exit 0
fi
for program in go kubectl; do
  if ! command -v "$program" >/dev/null 2>&1; then
    echo "Missing prerequisite: $program" >&2
    exit 1
  fi
done
if [[ ! -x "$LOCAL/bin/ko" || ! -s "$LOCAL/kubeconfig" ]]; then
  echo "Local AX controller build is not prepared; run bash ax_local/bootstrap.sh" >&2
  exit 1
fi
export PATH="$LOCAL/bin:$PATH"
export KUBECONFIG="$LOCAL/kubeconfig"
CONTEXT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["ax"]["context"])' "$ROOT/ax_local/config.json")"
if [[ "$CONTEXT" != kind-* || "$(kubectl config current-context)" != "$CONTEXT" ]]; then
  echo "Refusing to update a controller outside the configured local kind context" >&2
  exit 1
fi
VERSION="$(python3 "$PATCHER" "$LOCAL/src/ax")"
export KO_DOCKER_REPO="localhost:5001"
export KO_DEFAULTPLATFORMS="linux/$(go env GOARCH)"
echo "Rebuilding local AX controller for UID 10001 tools"
(cd "$LOCAL/src/ax" && ko apply -f deploy/ax-controller.yaml)
kubectl --context "$CONTEXT" set env deployment/ax-controller -n ax-system \
  AX_SNAPSHOTS_BUCKET=gs://ate-snapshots/popot-ax/
kubectl --context "$CONTEXT" rollout status deployment/ax-controller -n ax-system --timeout=300s
printf '%s\n' "$VERSION" > "$STAMP"

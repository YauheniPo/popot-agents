#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="localhost:5001/popot-agent-ax:local"
IMAGE_FILE="$ROOT/ax_local/.local/worker-image"

for program in docker go python3 kubectl; do
  if ! command -v "$program" >/dev/null 2>&1; then
    echo "Missing prerequisite: $program" >&2
    exit 1
  fi
done
BUILD_VERSIONS="$(python3 "$ROOT/ax_local/cluster/build_versions.py")"
eval "$BUILD_VERSIONS"
if [[ ! -f "$IMAGE_FILE" ]]; then
  echo "AX local cluster is not bootstrapped; run ax_local/bootstrap.sh first" >&2
  exit 1
fi
python3 "$ROOT/ax_local/cluster/worker_health.py"
bash "$ROOT/ax_local/cluster/controller_tools.sh"

docker build --platform "linux/$(go env GOARCH)" \
  --build-context "python-base=docker-image://$PYTHON_IMAGE" \
  --build-context "go-base=docker-image://$GO_IMAGE" \
  -f "$ROOT/ax_local/docker/worker.Dockerfile" -t "$IMAGE" "$ROOT"
docker push "$IMAGE"
IMAGE_DIGEST="$(docker image inspect "$IMAGE" --format '{{index .RepoDigests 0}}')"
if [[ ! "$IMAGE_DIGEST" =~ ^localhost:5001/popot-agent-ax@sha256:[0-9a-f]{64}$ ]]; then
  echo "Could not resolve the pushed worker image digest" >&2
  exit 1
fi
printf '%s\n' "$IMAGE_DIGEST" > "$IMAGE_FILE"

bash "$ROOT/ax_local/compose.sh" up --build --force-recreate -d --wait --wait-timeout 300 mcp-server
echo "AX worker and local API/MCP rebuilt"

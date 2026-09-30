#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="localhost:5001/popot-agent-ax:local"
IMAGE_FILE="$ROOT/ax_local/.local/worker-image"

for program in docker go; do
  if ! command -v "$program" >/dev/null 2>&1; then
    echo "Missing prerequisite: $program" >&2
    exit 1
  fi
done
if [[ ! -f "$IMAGE_FILE" ]]; then
  echo "AX local cluster is not bootstrapped; run ax_local/bootstrap.sh first" >&2
  exit 1
fi

docker build --platform "linux/$(go env GOARCH)" \
  -f "$ROOT/ax_local/docker/worker.Dockerfile" -t "$IMAGE" "$ROOT"
docker push "$IMAGE"
IMAGE_DIGEST="$(docker image inspect "$IMAGE" --format '{{index .RepoDigests 0}}')"
if [[ ! "$IMAGE_DIGEST" =~ ^localhost:5001/popot-agent-ax@sha256:[0-9a-f]{64}$ ]]; then
  echo "Could not resolve the pushed worker image digest" >&2
  exit 1
fi
printf '%s\n' "$IMAGE_DIGEST" > "$IMAGE_FILE"

docker compose --env-file "$ROOT/.env" -f "$ROOT/ax_local/compose.yaml" \
  up --build --force-recreate -d mcp-server
echo "AX worker and local API/MCP rebuilt"

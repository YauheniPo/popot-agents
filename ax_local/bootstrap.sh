#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCAL="${ROOT}/ax_local/.local"
AX_VERSION="v0.3.1"
AX_COMMIT="e70162a34037c221fe6fadefd98308c05a4ad8f3"
SUBSTRATE_COMMIT="672533541dbfcd29084e4de2475267088bda3651"
KO_VERSION="v0.19.1"
REGISTRY="localhost:5001"

for program in docker git go kubectl python3; do
  if ! command -v "$program" >/dev/null 2>&1; then
    echo "Missing prerequisite: $program" >&2
    exit 1
  fi
done
CLUSTER="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["ax"]["context"].removeprefix("kind-"))' "$ROOT/ax_local/config.json")"
ATESPACE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["ax"]["atespace"])' "$ROOT/ax_local/config.json")"
docker info >/dev/null
mkdir -p "$LOCAL/src" "$LOCAL/bin"
if [[ ! -x "$LOCAL/bin/ko" ]]; then
  echo "Installing ko ${KO_VERSION} into ${LOCAL}/bin"
  GOBIN="$LOCAL/bin" go install "github.com/google/ko@${KO_VERSION}"
fi
export PATH="$LOCAL/bin:$PATH"
export KUBECONFIG="$LOCAL/kubeconfig"

if [[ ! -d "$LOCAL/src/ax/.git" ]]; then
  git clone --depth 1 --branch "$AX_VERSION" https://github.com/google/ax.git "$LOCAL/src/ax"
fi
if [[ "$(git -C "$LOCAL/src/ax" rev-parse HEAD)" != "$AX_COMMIT" ]]; then
  echo "Unexpected AX checkout; expected $AX_VERSION at $AX_COMMIT" >&2
  exit 1
fi

if [[ ! -d "$LOCAL/src/substrate/.git" ]]; then
  git clone --depth 1 --branch main https://github.com/agent-substrate/substrate.git "$LOCAL/src/substrate"
fi
if [[ "$(git -C "$LOCAL/src/substrate" rev-parse HEAD)" != "$SUBSTRATE_COMMIT" ]]; then
  git -C "$LOCAL/src/substrate" fetch --depth 1 origin "$SUBSTRATE_COMMIT"
  git -C "$LOCAL/src/substrate" checkout --detach FETCH_HEAD
fi
if [[ "$(git -C "$LOCAL/src/substrate" rev-parse HEAD)" != "$SUBSTRATE_COMMIT" ]]; then
  echo "Substrate checkout does not match the AX v0.3.1 pin" >&2
  exit 1
fi

if [[ ! -s "$KUBECONFIG" ]]; then
  # Upstream create-kind-cluster.sh deletes a cluster with the selected name.
  # Refuse to run it when a node with our name already exists.
  if docker ps -a --format '{{.Names}}' | grep -qx "${CLUSTER}-control-plane"; then
    echo "Existing ${CLUSTER} node found without local kubeconfig; inspect it manually" >&2
    exit 1
  fi
  if docker ps -a --format '{{.Names}}' | grep -qx 'kind-registry' && \
     [[ "$(docker inspect -f '{{.State.Running}}' kind-registry)" != "true" ]]; then
    echo "Stopped kind-registry container exists; inspect it manually" >&2
    exit 1
  fi
  if docker ps --format '{{.Names}}' | grep -qx 'kind-registry' && \
     ! docker port kind-registry | grep -q '5001'; then
    echo "kind-registry already uses a different port; inspect it manually" >&2
    exit 1
  fi
  (cd "$LOCAL/src/substrate" && KIND_CLUSTER_NAME="$CLUSTER" hack/create-kind-cluster.sh)
fi
if [[ "$(kubectl config current-context)" != "kind-${CLUSTER}" ]]; then
  echo "Local kubeconfig does not select kind-${CLUSTER}" >&2
  exit 1
fi
python3 "$ROOT/ax_local/container_kubeconfig.py" "$KUBECONFIG" "$LOCAL/kubeconfig-container"

if ! kubectl --context "kind-${CLUSTER}" get service api -n ate-system >/dev/null 2>&1; then
  (cd "$LOCAL/src/substrate" && KIND_CLUSTER_NAME="$CLUSTER" \
    KUBECTL_CONTEXT="kind-${CLUSTER}" hack/install-ate-kind.sh --deploy-ate-system)
fi
if [[ -z "$(kubectl --context "kind-${CLUSTER}" get workerpool -n "$ATESPACE" -o name 2>/dev/null || true)" ]]; then
  if [[ "$ATESPACE" != "ate-demo-counter" ]]; then
    echo "Configure a WorkerPool in AX atespace $ATESPACE before bootstrapping" >&2
    exit 1
  fi
  (cd "$LOCAL/src/substrate" && KIND_CLUSTER_NAME="$CLUSTER" \
    KUBECTL_CONTEXT="kind-${CLUSTER}" hack/install-ate-kind.sh --deploy-demo-counter)
fi

(cd "$LOCAL/src/ax" && go build -o "$LOCAL/bin/ax" ./cmd/ax)
export KO_DOCKER_REPO="$REGISTRY"
export KO_DEFAULTPLATFORMS="linux/$(go env GOARCH)"
kubectl --context "kind-${CLUSTER}" apply -f "$LOCAL/src/ax/deploy/redis.yaml"
(cd "$LOCAL/src/ax" && ko apply -f deploy/ax-controller.yaml)
(cd "$LOCAL/src/ax" && ko apply -f deploy/ax-server.yaml)
kubectl --context "kind-${CLUSTER}" set env deployment/ax-controller -n ax-system \
  AX_SNAPSHOTS_BUCKET=gs://ate-snapshots/popot-ax/
kubectl --context "kind-${CLUSTER}" rollout status deployment/ax-controller -n ax-system --timeout=300s
kubectl --context "kind-${CLUSTER}" rollout status deployment/ax-server -n ax-system --timeout=300s

IMAGE="$REGISTRY/popot-agent-ax:local"
docker build --platform "linux/$(go env GOARCH)" -f "$ROOT/ax_local/docker/worker.Dockerfile" -t "$IMAGE" "$ROOT"
docker push "$IMAGE"
IMAGE_DIGEST="$(docker image inspect "$IMAGE" --format '{{index .RepoDigests 0}}')"
if [[ ! "$IMAGE_DIGEST" =~ ^localhost:5001/popot-agent-ax@sha256:[0-9a-f]{64}$ ]]; then
  echo "Could not resolve the pushed worker image digest" >&2
  exit 1
fi
printf '%s\n' "$IMAGE_DIGEST" > "$LOCAL/worker-image"

"$LOCAL/bin/ax" --context="kind-${CLUSTER}" --atespace="$ATESPACE" get tasks
echo "AX local worker image: $IMAGE_DIGEST"
echo "AX cluster and worker image prepared"

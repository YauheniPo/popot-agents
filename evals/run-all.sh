#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
RUN_NAME="${1:-full-$(date -u +%Y%m%dT%H%M%SZ)}"
if (( $# )); then shift; fi
if [[ ! "$RUN_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$ ]]; then
  echo "Run name must be a new directory name containing letters, digits, dots, underscores or hyphens" >&2
  exit 2
fi
OUTPUT="${POPOT_EVAL_OUTPUT:-$ROOT/reports/evals/$RUN_NAME}"
mkdir -p "$(dirname "$OUTPUT")"
mkdir "$OUTPUT"
SOURCE_REVISION="$(git rev-parse HEAD)"
if [[ -n "$(git status --porcelain)" ]]; then
  SOURCE_REVISION="$SOURCE_REVISION (dirty; see frozen source hashes)"
fi

if [[ "${POPOT_EVAL_SKIP_RUNTIME:-0}" != 1 ]]; then
  docker exec -w /opt/popot popot-evals-phoenix-1 python -m evals.runtime \
    --output "/tmp/$RUN_NAME-runtime" --hours 12 --annotate --api-key-env PHOENIX_ADMIN_SECRET
  docker cp "popot-evals-phoenix-1:/tmp/$RUN_NAME-runtime" "$OUTPUT/runtime"
fi

docker exec popot-ax-local-api-1 python -c "from pathlib import Path; Path('/app/evals').mkdir(exist_ok=True)"
docker cp "$ROOT/evals/." popot-ax-local-api-1:/app/evals/
if docker exec -i popot-ax-local-api-1 python -u -m evals.baseline \
    --output "/tmp/$RUN_NAME-baseline" --source-revision "$SOURCE_REVISION" --monitor "$@"; then
  docker cp "popot-ax-local-api-1:/tmp/$RUN_NAME-baseline" "$OUTPUT/baseline"
else
  result=$?
  docker cp "popot-ax-local-api-1:/tmp/$RUN_NAME-baseline" "$OUTPUT/baseline" || true
  exit "$result"
fi

docker cp "$ROOT/evals/gateway.mjs" "popot-ax-local-omniroute-1:/tmp/$RUN_NAME-gateway.mjs"
docker cp "$OUTPUT/baseline/report.json" "popot-ax-local-omniroute-1:/tmp/$RUN_NAME-report.json"
docker exec popot-ax-local-omniroute-1 node "/tmp/$RUN_NAME-gateway.mjs" "/tmp/$RUN_NAME-report.json" \
  > "$OUTPUT/baseline/gateway.json"
python3 -m evals.report "$OUTPUT/baseline" "$OUTPUT/baseline/gateway.json"

docker exec popot-evals-phoenix-1 python -c "from pathlib import Path; Path('/tmp/$RUN_NAME-code/popot_agents').mkdir(parents=True)"
docker cp "$ROOT/evals" "popot-evals-phoenix-1:/tmp/$RUN_NAME-code/"
docker cp "$ROOT/popot_agents/telemetry.py" "popot-evals-phoenix-1:/tmp/$RUN_NAME-code/popot_agents/telemetry.py"
docker cp "$ROOT/popot_agents/progress.py" "popot-evals-phoenix-1:/tmp/$RUN_NAME-code/popot_agents/progress.py"
docker cp "$OUTPUT/baseline" "popot-evals-phoenix-1:/tmp/$RUN_NAME-baseline"
PHOENIX_ARGS=(python -u -m evals.phoenix "/tmp/$RUN_NAME-baseline")
if [[ "${POPOT_EVAL_BASELINE:-0}" == 1 ]]; then PHOENIX_ARGS+=(--baseline); fi
docker exec -i -w "/tmp/$RUN_NAME-code" popot-evals-phoenix-1 \
  "${PHOENIX_ARGS[@]}"
docker cp "popot-evals-phoenix-1:/tmp/$RUN_NAME-baseline/phoenix.json" "$OUTPUT/baseline/phoenix.json"
python3 -m evals.metrics "$OUTPUT/baseline"
printf 'Eval reports: %s\n' "$OUTPUT"

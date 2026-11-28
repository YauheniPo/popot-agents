#!/usr/bin/env bash
set -euo pipefail
# Bash otherwise keeps reading this file after long-running commands return.
if [[ "${POPOT_EVAL_SCRIPT_SNAPSHOT:-}" != "$0" ]]; then
  export POPOT_EVAL_SCRIPT_SNAPSHOT="$0"
  exec bash -c "$(< "$0")" "$0" "$@"
fi

ROOT="${POPOT_EVAL_REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
EVALS="${POPOT_EVAL_LAUNCHER_ROOT:-$ROOT/evals}"
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

CODE="/tmp/$RUN_NAME-code"
docker exec popot-ax-local-api-1 python -c "from pathlib import Path; Path('$CODE').mkdir(exist_ok=True)"
docker cp "$EVALS" "popot-ax-local-api-1:$CODE/"
if docker exec -i -w "$CODE" -e "PYTHONPATH=$CODE:/app" -e POPOT_EVAL_SOURCE_ROOT=/app \
    popot-ax-local-api-1 python -u -m evals.baseline \
    --output "/tmp/$RUN_NAME-baseline" --source-revision "$SOURCE_REVISION" --monitor "$@"; then
  docker cp "popot-ax-local-api-1:/tmp/$RUN_NAME-baseline" "$OUTPUT/baseline"
else
  result=$?
  docker cp "popot-ax-local-api-1:/tmp/$RUN_NAME-baseline" "$OUTPUT/baseline" || true
  exit "$result"
fi

PHOENIX_ARGS=("$EVALS/publish.sh" "$OUTPUT/baseline")
if [[ "${POPOT_EVAL_BASELINE:-0}" == 1 ]]; then PHOENIX_ARGS+=(--baseline); fi
bash "${PHOENIX_ARGS[@]}"

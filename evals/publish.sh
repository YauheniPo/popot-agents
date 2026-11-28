#!/usr/bin/env bash
# Publish saved measurements without starting agents or judge calls.
set -euo pipefail
# Bash otherwise keeps reading this file after long-running commands return.
if [[ "${POPOT_EVAL_SCRIPT_SNAPSHOT:-}" != "$0" ]]; then
  export POPOT_EVAL_SCRIPT_SNAPSHOT="$0"
  exec bash -c "$(< "$0")" "$0" "$@"
fi

ROOT="${POPOT_EVAL_REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
EVALS="${POPOT_EVAL_LAUNCHER_ROOT:-$ROOT/evals}"
DIRECTORY="$(cd "${1:?Usage: bash evals/publish.sh BASELINE_DIRECTORY [--baseline]}" && pwd)"
shift
cd "$ROOT"
export KUBECONFIG="${KUBECONFIG:-$ROOT/ax_local/.local/kubeconfig}"

if [[ ! -f "$DIRECTORY/report.json" || ! -f "$DIRECTORY/config.json" ]]; then
  echo "Saved report.json and config.json are required" >&2
  exit 2
fi

if [[ ! -f "$DIRECTORY/gateway.json" ]]; then
  RUN_NAME="publish-$$"
  docker cp "$EVALS/gateway.mjs" "popot-ax-local-omniroute-1:/tmp/$RUN_NAME-gateway.mjs"
  docker cp "$DIRECTORY/report.json" "popot-ax-local-omniroute-1:/tmp/$RUN_NAME-report.json"
  docker exec popot-ax-local-omniroute-1 node "/tmp/$RUN_NAME-gateway.mjs" "/tmp/$RUN_NAME-report.json" \
    > "$DIRECTORY/gateway.json.tmp"
  mv "$DIRECTORY/gateway.json.tmp" "$DIRECTORY/gateway.json"
fi
(cd "$(dirname "$EVALS")"; PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 -m evals.report "$DIRECTORY" "$DIRECTORY/gateway.json")

# The SDK must not share the server's memory limit. The bind preserves the
# journal even if either container exits partway through publication.
bash "$ROOT/compose.sh" --env-file "$ROOT/.env" -f "$ROOT/evals/compose.yaml" run --rm --no-deps -T \
  --volume "$DIRECTORY:/eval-run" --volume "$EVALS:/opt/popot/evals:ro" phoenix-import -u -m evals.phoenix /eval-run \
  --endpoint http://phoenix:6006 "$@"
(cd "$(dirname "$EVALS")"; PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 -m evals.metrics "$DIRECTORY")
printf 'Eval reports: %s\n' "$DIRECTORY"

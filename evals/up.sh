#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
bash "$ROOT/compose.sh" --env-file "$ROOT/.env" -f "$ROOT/evals/compose.yaml" up -d --force-recreate --wait --wait-timeout 120 phoenix
bash "$ROOT/compose.sh" --env-file "$ROOT/.env" -f "$ROOT/evals/compose.yaml" run --rm --no-deps phoenix-init

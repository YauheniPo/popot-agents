#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$ROOT/ax_local/cluster/build_versions.py" compose "$@"

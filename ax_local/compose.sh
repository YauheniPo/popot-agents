#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FILES=(-f "$ROOT/ax_local/compose.yaml")
USES_OMNIROUTE="$(python3 -c '
import json, sys
roles = json.load(open(sys.argv[1]))
print(int(any(role.get("agent") in ("omniroute", "omniroute_code") for role in roles.values())))
' "$ROOT/config/roles.json")"
if [[ "$USES_OMNIROUTE" == 1 ]]; then
  FILES+=(-f "$ROOT/ax_local/compose.omniroute.yaml" --profile omniroute)
fi
exec bash "$ROOT/compose.sh" --env-file "$ROOT/.env" "${FILES[@]}" "$@"

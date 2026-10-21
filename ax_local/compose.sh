#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FILES=(-f "$ROOT/ax_local/compose.yaml")
USES_OMNIROUTE="$(python3 -c '
import json, sys
from pathlib import Path
roles = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(Path(sys.argv[1]).glob("*.json"))]
print(int(any(role.get("agent") in ("omniroute", "omniroute_code") for role in roles)))
' "$ROOT/config/roles")"
if [[ "$USES_OMNIROUTE" == 1 ]]; then
  FILES+=(-f "$ROOT/ax_local/compose.omniroute.yaml" --profile omniroute)
fi
exec bash "$ROOT/compose.sh" --env-file "$ROOT/.env" "${FILES[@]}" "$@"

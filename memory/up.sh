#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 "$ROOT/memory/setup.py"
FILES=(-f "$ROOT/memory/compose.yaml")
if docker network inspect kind >/dev/null 2>&1; then
  FILES+=(-f "$ROOT/memory/compose.ax.yaml")
fi
docker compose --env-file "$ROOT/.env" "${FILES[@]}" up --build -d --wait --wait-timeout 360
docker compose --env-file "$ROOT/.env" "${FILES[@]}" exec -T cognee \
  python /app/popot-sync/import_skills.py
echo "Cognee graph UI: http://localhost:3001"
echo "Login: default_user@example.com; password is COGNEE_PASSWORD in .env"

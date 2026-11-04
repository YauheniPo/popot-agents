#!/usr/bin/env bash
set -euo pipefail

: "${TASK:?Set TASK to the request text}"
ROLE="${ROLE:-}"
SESSION_ID="${SESSION_ID:-}"
CALL_ID="${CALL_ID:-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')}"
PROGRESS_TOKEN="${PROGRESS_TOKEN:-$CALL_ID}"

PAYLOAD="$(python3 - "$TASK" "$ROLE" "$SESSION_ID" "$CALL_ID" "$PROGRESS_TOKEN" <<'PY'
import json
import sys

task, role, session_id, call_id, progress_token = sys.argv[1:]
arguments = {'sessionId': session_id, 'task': task}
if not session_id and role:
    arguments['role'] = role
print(json.dumps({
    'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
    'params': {
        '_meta': {'progressToken': progress_token, 'callId': call_id},
        'name': 'run_task',
        'arguments': arguments,
    },
}, ensure_ascii=False))
PY
)"

CURL_ARGS=(
  -N --fail-with-body -sS 'http://127.0.0.1:8003/mcp'
  -H 'Content-Type: application/json'
  -H 'Accept: application/json, text/event-stream'
  -H 'MCP-Protocol-Version: 2025-11-25'
  --data-binary "$PAYLOAD"
)
python3 - curl "${CURL_ARGS[@]}" <<'PY'
import shlex
import sys

print('Command to execute:')
print(' '.join(shlex.quote(arg) for arg in sys.argv[1:]))
PY

RESPONSE_FILE="$(mktemp)"
trap 'rm -f "$RESPONSE_FILE"' EXIT
if curl "${CURL_ARGS[@]}" | tee "$RESPONSE_FILE"; then
  :
else
  status=$?
  printf 'curl/tee failed (exit %s)\n' "$status" >&2
  exit "$status"
fi

python3 - "$RESPONSE_FILE" <<'PY'
import json
from pathlib import Path
import sys

result = None
for line in Path(sys.argv[1]).read_text().splitlines():
    if not line.startswith('data: '):
        continue
    try:
        message = json.loads(line[6:])
    except ValueError:
        continue
    if message.get('id') == 1:
        result = message
if result is None:
    sys.exit('MCP returned no final result')
if result.get('error') or result.get('result', {}).get('isError'):
    sys.exit('MCP tool returned an error; see the response above')
task_result = None
queued = None
for item in result.get('result', {}).get('content', []):
    if item.get('type') != 'text':
        continue
    try:
        parsed = json.loads(item['text'])
    except (KeyError, ValueError):
        continue
    if isinstance(parsed, dict) and isinstance(parsed.get('outcome'), dict):
        task_result = parsed
        break
    if isinstance(parsed, dict) and parsed.get('status') in {'queued', 'delivered'}:
        queued = parsed
if queued is not None:
    print(f"Comment {queued.get('sessionId', '?')}: {queued['status']} "
          f"(interventionId={queued.get('interventionId', '?')})", file=sys.stderr)
    sys.exit(0)
if task_result is None:
    sys.exit('MCP returned no task outcome')
status = task_result['outcome'].get('status')
reason = task_result['outcome'].get('reason')
print(f"Task {task_result.get('sessionId', '?')}: {status or 'unknown'}"
      + (f" ({reason})" if reason else ''), file=sys.stderr)
if status != 'completed':
    sys.exit(1)
PY

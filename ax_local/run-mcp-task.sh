#!/usr/bin/env bash
set -euo pipefail

ROLE="${ROLE:-}"
SESSION_ID="${SESSION_ID:-}"
DISABLE_DELEGATION="${DISABLE_DELEGATION:-}"
CALL_ID="${CALL_ID:-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')}"
PROGRESS_TOKEN="${PROGRESS_TOKEN:-$CALL_ID}"

PAYLOAD="$(python3 - "$ROLE" "$SESSION_ID" "$CALL_ID" "$PROGRESS_TOKEN" "$DISABLE_DELEGATION" 3<&0 <<'PY'
import json
import os
import sys

role, session_id, call_id, progress_token, disable_delegation = sys.argv[1:]
try:
    task = os.environb.get(b'TASK', b'').decode('utf-8')
    if not task:
        with os.fdopen(3, encoding='utf-8', newline='') as source:
            if source.isatty():
                print('Paste the task text, then press Ctrl+D on an empty line:', file=sys.stderr)
            task = source.read()
    task.encode('utf-8')
except UnicodeError:
    sys.exit('TASK must be valid UTF-8 text; no request was sent')
if not task.strip():
    sys.exit('Set TASK or provide the request text on stdin; it must contain non-whitespace text')
if '\x00' in task:
    sys.exit('TASK cannot contain NUL characters; no request was sent')
arguments = {'sessionId': session_id, 'task': task}
if not session_id and role:
    arguments['role'] = role
if disable_delegation:
    if disable_delegation not in {'true', 'false'}:
        sys.exit('DISABLE_DELEGATION must be true or false')
    arguments['disableDelegation'] = disable_delegation == 'true'
print(json.dumps({
    'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
    'params': {
        '_meta': {'progressToken': progress_token, 'callId': call_id},
        'name': 'run_task',
        'arguments': arguments,
    },
}))
PY
)"

CURL_ARGS=(
  -N --fail-with-body -sS 'http://127.0.0.1:8003/mcp'
  -H 'Content-Type: application/json'
  -H 'Accept: application/json, text/event-stream'
  -H 'MCP-Protocol-Version: 2025-11-25'
  --data-binary @-
)
python3 - curl "${CURL_ARGS[@]}" 3<<<"$PAYLOAD" <<'PY'
import json
import os
import shlex
import sys

sys.stdout.reconfigure(encoding='utf-8')
print('Command to execute:')
print(' '.join(shlex.quote(arg) for arg in sys.argv[1:]) + " <<'POPOT_MCP_REQUEST'")
with os.fdopen(3, encoding='ascii') as source:
    print(json.dumps(json.load(source), ensure_ascii=False))
print('POPOT_MCP_REQUEST')
PY

RESPONSE_FILE="$(mktemp)"
trap 'rm -f "$RESPONSE_FILE"' EXIT
if printf '%s' "$PAYLOAD" | curl "${CURL_ARGS[@]}" | tee "$RESPONSE_FILE"; then
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
# SSE lines use LF; Unicode separators inside JSON belong to the message.
for line in Path(sys.argv[1]).read_text(encoding='utf-8').split('\n'):
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

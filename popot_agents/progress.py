"""Forward native harness events without translating or selecting event names."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import os
import re
import sys
import threading
import time

_observer = ContextVar('agent_progress', default=None)


class TurnProgress:
    """Bounded, payload-free snapshot for concurrent status readers."""

    def __init__(self, session_id=None):
        self._session_id = session_id
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._state = {
            'state': 'running', 'startedAt': datetime.now(timezone.utc).isoformat(),
            'sequence': 0, 'modelCalls': 0, 'toolCalls': 0,
            'lastAction': None, 'lastError': None,
        }

    def update(self, message, *, finished=False):
        if not is_progress_message(message):
            return
        value = json.loads(message)
        event = value['event']
        if event in {'output', 'assistant_reasoning', 'assistant_message', 'assistant_answer'}:
            return
        action = {'event': redact_text(event)[:160]}
        for key in ('name', 'model', 'server', 'agent_role', 'agent_session_id', 'error_type',
                    'code', 'stage', 'phase', 'reason'):
            if isinstance(value.get(key), str):
                action[key] = redact_text(value[key])[:160]
        if (event == 'tool_error' and value.get('recoverable') is True
                and value.get('error_type') in {'ToolInputError', 'MCPToolError'}
                and isinstance(value.get('error'), str)):
            action['message'] = redact_text(value['error'])[:400]
        for key in ('round', 'attempt'):
            if type(value.get(key)) is int and 0 <= value[key] <= 1000000:
                action[key] = value[key]
        with self._lock:
            self._state['sequence'] += 1
            action['sequence'] = self._state['sequence']
            action['observedAt'] = datetime.now(timezone.utc).isoformat()
            if 'agent_session_id' not in action and 'agent_role' not in action and self._session_id:
                action['agent_session_id'] = self._session_id
            self._state['updatedAt'] = datetime.now(timezone.utc).isoformat()
            self._state['lastAction'] = action
            if event == 'model_request':
                self._state['modelCalls'] += 1
            elif event == 'tool_call':
                self._state['toolCalls'] += 1
            if event.endswith(('_error', '_failed', '_timeout')) or event == 'failed':
                self._state['lastError'] = dict(action)
            if finished and event in {'completed', 'failed'}:
                self._state['state'] = event
                self._state['elapsedSeconds'] = round(time.monotonic() - self._started, 1)

    def snapshot(self):
        with self._lock:
            result = dict(self._state)
            for key in ('lastAction', 'lastError'):
                if result[key] is not None:
                    result[key] = dict(result[key])
            if result['state'] == 'running':
                result['elapsedSeconds'] = round(time.monotonic() - self._started, 1)
            result['eventsAfterLastError'] = (result['sequence'] - result['lastError']['sequence']
                                               if result['lastError'] else None)
            return result


def redact_text(text):
    for name, value in os.environ.items():
        if len(value) >= 4 and re.search(r'(TOKEN|PASSWORD|SECRET|_KEY)$', name):
            text = text.replace(value, '[REDACTED]')
    text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----',
                  '[REDACTED]', text, flags=re.S)
    text = re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{20,})',
                  '[REDACTED]', text)
    text = re.sub(r'(?i)(\b(?:password|passwd|pasw|token|api[_-]?key|secret|пароль)\b\s*[:=]\s*)(?:"[^"]*"|\x27[^\x27]*\x27|[^\s,;]+)',
                  r'\1[REDACTED]', text)
    text = re.sub(r'(?i)(authorization\s*:\s*bearer\s+)\S+', r'\1[REDACTED]', text)
    return re.sub(r'(https?://)[^\s/@]+:[^\s/@]+@', r'\1[REDACTED]@', text)


def _safe_fields(value):
    if isinstance(value, dict):
        return {key: '[REDACTED]' if re.search(
                    r'(?i)(?:^|[_-])(?:authorization|cookie|password|passwd|secret|token|api[_-]?key|private[_-]?key)$', key)
                else _safe_fields(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_fields(item) for item in value]
    return redact_text(value) if isinstance(value, str) else value


def format_progress(event, **fields):
    return json.dumps(_safe_fields({'event': event, **fields}), ensure_ascii=False)


def is_progress_message(message):
    if not isinstance(message, str):
        return False
    try:
        value = json.loads(message)
    except (ValueError, RecursionError):
        return False
    return isinstance(value, dict) and isinstance(value.get('event'), str) and bool(value['event'])


def scoped_progress(role, message, *, session_id=None):
    if not is_progress_message(message):
        return None
    value = json.loads(message)
    # Preserve the originating role through nested delegation. The harness may
    # itself use a "role" field, so keep source attribution in a separate field.
    value.setdefault('agent_role', role)
    if session_id and value['agent_role'] == role:
        value.setdefault('agent_session_id', session_id)
    return json.dumps(value, ensure_ascii=False)


def progress_observer():
    return _observer.get()


@contextmanager
def observe_progress(callback):
    token = _observer.set(callback)
    try:
        yield
    finally:
        _observer.reset(token)


def forward_progress(message):
    callback = _observer.get()
    if callback is not None and is_progress_message(message):
        callback(message)


def emit_progress(event, **fields):
    if _observer.get() is not None:
        forward_progress(format_progress(event, **fields))


def trace_event(event, **fields):
    message = format_progress(event, **fields)
    forward_progress(message)
    print(message, file=sys.stderr, flush=True)


def trace_output(text):
    emit_progress('output', stream='stderr', content=text)
    print(redact_text(text), file=sys.stderr, flush=True)

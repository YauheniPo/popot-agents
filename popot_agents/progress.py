"""Forward native harness events without translating or selecting event names."""

from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
import re
import sys

_observer = ContextVar('agent_progress', default=None)


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


def scoped_progress(role, message):
    if not is_progress_message(message):
        return None
    value = json.loads(message)
    # Preserve the originating role through nested delegation. The harness may
    # itself use a "role" field, so keep source attribution in a separate field.
    value.setdefault('agent_role', role)
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

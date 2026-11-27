"""Bounded, redacted payloads shared by runtime and offline trace exporters."""

import hashlib
import json
import re

from popot_agents.progress import redact_text


VERSION = 'popot-telemetry-v2'
PAYLOAD_LIMIT = 1024 * 1024
_SECRET_KEYS = {'authorization', 'cookie', 'setcookie', 'password', 'passwd',
                'secret', 'token', 'apikey', 'accesstoken', 'refreshtoken',
                'privatekey', 'clientsecret', 'credential', 'credentials', 'mcpsessionid', 'proxyauthorization'}


def _secret_key(key):
    normalized = re.sub(r'[^a-z0-9]', '', str(key).lower())
    return normalized in _SECRET_KEYS or normalized.endswith(('password', 'apikey', 'privatekey', 'secret', 'token'))


def _credential_pair(value):
    return (isinstance(value, (list, tuple)) and len(value) == 2
            and isinstance(value[0], str) and isinstance(value[1], str) and _secret_key(value[0]))


def sensitive_values(value):
    """Retain credential values only in memory to redact unlabelled echoes."""
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if _secret_key(key) and isinstance(item, str) and item:
                found.add(item)
            found.update(sensitive_values(item))
    elif _credential_pair(value):
        if value[1]:
            found.add(value[1])
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.update(sensitive_values(item))
    elif isinstance(value, str) and value.lstrip().startswith(('{', '[')):
        try:
            found.update(sensitive_values(json.loads(value)))
        except ValueError:
            pass
    return found


def safe_value(value, extra_secrets=()):
    if isinstance(value, dict):
        return {str(key): '[REDACTED]' if _secret_key(key) else safe_value(item, extra_secrets)
                for key, item in value.items()}
    if _credential_pair(value):
        return [value[0], '[REDACTED]']
    if isinstance(value, (list, tuple)):
        return [safe_value(item, extra_secrets) for item in value]
    if isinstance(value, str):
        if value.lstrip().startswith(('{', '[')):
            try:
                parsed = json.loads(value)
                clean = safe_value(parsed, extra_secrets)
                return value if clean == parsed else json.dumps(clean, ensure_ascii=False)
            except ValueError:
                pass
        for secret in sorted(extra_secrets, key=len, reverse=True):
            value = value.replace(secret, '[REDACTED]')
        return redact_text(value)
    return value


def decode_payload(value):
    if isinstance(value, bytes):
        try:
            return json.loads(value)
        except (ValueError, UnicodeError):
            return value.decode('utf-8', errors='replace')
    return value


def payload_attributes(side, value, *, limit=PAYLOAD_LIMIT, extra_secrets=()):
    value = decode_payload(value)
    clean = safe_value(value, set(extra_secrets) | sensitive_values(value))
    encoded = json.dumps(clean, ensure_ascii=False, separators=(',', ':')).encode()
    truncated = len(encoded) > limit
    result = encoded
    if truncated:
        # Escaping the preview can expand it. Find a valid UTF-8 JSON envelope
        # within the limit instead of cutting a serialized JSON string in half.
        low, high = 0, min(len(encoded), limit)
        while low < high:
            middle = (low + high + 1) // 2
            preview = encoded[:middle].decode('utf-8', errors='ignore')
            candidate = json.dumps({'truncated': True, 'preview': preview}, ensure_ascii=False).encode()
            if len(candidate) <= limit:
                low = middle
            else:
                high = middle - 1
        result = json.dumps({'truncated': True, 'preview': encoded[:low].decode('utf-8', errors='ignore')}, ensure_ascii=False).encode()
    return {side + '.value': result.decode(), side + '.mime_type': 'application/json',
            'popot.' + side + '.truncated': truncated,
            'popot.' + side + '.bytes': len(encoded),
            'popot.' + side + '.sha256': hashlib.sha256(encoded).hexdigest(),
            'popot.telemetry.version': VERSION}


def message_attributes(side, messages):
    """OpenInference message indexing; full envelope remains in input/output.value."""
    attrs = {}
    if not isinstance(messages, list):
        return attrs
    for index, message in enumerate(messages[:32]):
        if not isinstance(message, dict):
            continue
        prefix = f'llm.{side}_messages.{index}.message.'
        for key in ('role', 'content', 'name', 'tool_call_id'):
            value = message.get(key)
            if value is not None:
                attrs[prefix + key] = (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))[:16000]
        calls = message.get('tool_calls')
        for number, call in enumerate(calls[:16] if isinstance(calls, list) else []):
            if not isinstance(call, dict):
                continue
            call_prefix = prefix + f'tool_calls.{number}.tool_call.'
            if isinstance(call.get('id'), str):
                attrs[call_prefix + 'id'] = call['id']
            for key in ('name', 'arguments'):
                function = call.get('function')
                value = function.get(key) if isinstance(function, dict) else None
                if value is not None:
                    attrs[call_prefix + 'function.' + key] = str(value)[:16000]
    return attrs

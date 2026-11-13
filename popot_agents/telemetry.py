"""Bounded original payloads shared by runtime and offline trace exporters."""

import hashlib
import json


VERSION = 'popot-telemetry-v2'
PAYLOAD_LIMIT = 1024 * 1024


def decode_payload(value):
    if isinstance(value, bytes):
        try:
            return json.loads(value)
        except (ValueError, UnicodeError):
            return value.decode('utf-8', errors='replace')
    return value


def payload_attributes(side, value, *, limit=PAYLOAD_LIMIT):
    value = decode_payload(value)
    encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
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

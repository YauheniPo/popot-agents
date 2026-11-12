"""Validate final reports and retain bounded receipts when formatting fails."""
import hashlib
import json
import re

from popot_agents.runtime_config import RUNTIME
from popot_agents.prompts import render_prompt
from .turn_memory import scrub
from .verification import validate_findings


def without_format_repairs(messages):
    """Retire request-only system instructions saved by older workers."""
    marker = 'FORMAT_ERROR_REASON'
    prefix, suffix = render_prompt('reporting/repair', reason=marker, result_contract='').split(marker, 1)
    return [message for message in messages if not (
        message.get('role') == 'system' and isinstance(message.get('content'), str)
        and message['content'].startswith(prefix) and suffix in message['content'][len(prefix):])]


def evidence_transcript(messages, context=()):
    """Keep ordered receipts and scope, excluding the model's self-confirming prose."""
    names = {call.get('id'): call.get('function', {}).get('name', 'unknown')
             for message in messages for call in message.get('tool_calls', [])}
    transcript = []
    for message in messages:
        role = message.get('role')
        if role == 'assistant' and message not in context:
            if message.get('tool_calls'):
                transcript.append({'source': 'tool_calls_not_proof_of_execution',
                                   'tool_calls': message['tool_calls']})
        elif role == 'tool':
            name = names.get(message.get('tool_call_id'), 'unknown')
            source = ('model_reported_progress' if name == 'review_checkpoint' else
                      'historical_memory' if name.startswith('mcp__cognee__') else
                      'delegated_report' if name == 'delegate_task' else 'tool_result')
            transcript.append({'source': source, 'tool': name,
                               'call_id': message.get('tool_call_id'),
                               'content': message.get('content')})
        else:
            transcript.append({'source': 'scope_or_context_not_execution_evidence',
                               'role': role, 'content': message.get('content')})
    return transcript


def _next_quote_state(quoted, escaped, char):
    """Track whether the scanner sits inside a JSON string after ``char``."""
    if not quoted:
        return char == '"', False
    if escaped:
        return True, False
    if char == '\\':
        return True, True
    return char != '"', False


def _closes_container(text, index):
    """Detect a comma whose next non-space character ends an array or object."""
    following = index + 1
    while following < len(text) and text[following] in ' \t\r\n':
        following += 1
    return following < len(text) and text[following] in '}]'


def _without_trailing_commas(text):
    result = []
    quoted = escaped = False
    previous = ''
    for index, char in enumerate(text):
        was_quoted = quoted
        quoted, escaped = _next_quote_state(quoted, escaped, char)
        if not was_quoted and char == ',' and previous not in '[{,:' and _closes_container(text, index):
            continue
        result.append(char)
        if char not in ' \t\r\n':
            previous = char
    return ''.join(result)


def parse_result(content, finish_reason=None):
    if finish_reason not in (None, 'stop'):
        raise ValueError('truncated_result' if finish_reason == 'length' else 'unexpected_finish_reason')
    if not isinstance(content, str) or not content.strip():
        raise ValueError('empty_content')
    text = content.strip().lstrip('\ufeff').strip()
    fence = re.fullmatch(r'```(?:json)?[ \t]*\r?\n(.*?)\r?\n```', text, re.I | re.S)
    if fence:
        text = fence[1]
    try:
        report = json.loads(text)
    except ValueError:
        # Normalize only separators and literal text whitespace. Never invent
        # missing fields, closing delimiters or escapes for ambiguous quotes.
        text = _without_trailing_commas(text)
        try:
            if re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', text):
                raise ValueError('invalid_control_character')
            report = json.loads(text, strict=False)
        except ValueError:
            raise ValueError('invalid_json') from None
    if not isinstance(report, dict):
        raise ValueError('expected_object')
    if report.get('status') not in ('completed', 'partial', 'blocked', 'failed'):
        raise ValueError('invalid_status')
    if not isinstance(report.get('answer'), str) or not report['answer'].strip():
        raise ValueError('missing_answer')
    for key in ('evidence', 'remaining_work', 'errors'):
        if not isinstance(report.get(key), list) or any(not isinstance(i, str) for i in report[key]):
            raise ValueError('invalid_' + key)
        report[key] = [i.strip() for i in report[key] if i.strip()]
    if report['status'] == 'completed' and (report['remaining_work'] or report['errors']):
        report['status'] = 'partial'
    if 'revision' in report and (not isinstance(report['revision'], str) or not re.fullmatch(r'[a-f0-9]{40}', report['revision'])):
        raise ValueError('invalid_revision')
    if 'verification_status' in report and report['verification_status'] not in ('passed', 'failed'):
        raise ValueError('invalid_verification_status')
    if 'findings' in report:
        validate_findings(report['findings'])
    return report


def _child_receipts(completed_children, chars):
    receipts = []
    for child in completed_children.values():
        label = f"Child {child['role']} (sessionId={child['sessionId']}) reported: "
        receipts.append(label + scrub(child['answer'])[:chars])
        for item in child.get('outcome', {}).get('evidence', []):
            if isinstance(item, str) and item.strip():
                receipts.append(label + scrub(item)[:chars])
    return receipts


def _tool_name_index(messages):
    return {call.get('id'): call.get('function', {}).get('name', 'tool')
            for m in messages for call in m.get('tool_calls', [])}


def _is_own_receipt(name):
    """Delegation and memory traffic is reported by the server, not as raw receipts."""
    return name in {'delegate_task', 'review_checkpoint'} or name.startswith('mcp__cognee__')


def _tool_receipt(message, name, chars):
    content = message.get('content')
    if not isinstance(content, str) or not content.strip():
        return None
    preview = ' | '.join(scrub(content).splitlines()[:3])[:min(chars, 240)]
    artifact = re.search(r'Full output: (\.tool-results/[a-f0-9]{32}\.txt)', content)
    return (f'Tool receipt {name} ({message.get("tool_call_id")}); '
            + f'outcome requires interpretation: {preview}'
            + f' [sha256={hashlib.sha256(content.encode()).hexdigest()}]'
            + (f' Full output: {artifact[1]}' if artifact else ''))


def retained_receipts(messages, completed_children, observations=()):
    """Receipts are attributed observations, never proof of whole-task completion."""
    limit = RUNTIME['memory']['tool_evidence_count']
    chars = RUNTIME['memory']['tool_evidence_chars']
    # Check outcomes survive transcript compaction and later file reads. An
    # unbound check remains useful evidence, but cannot verify a specific SHA.
    receipts = [_observation_receipt(item, chars) for item in reversed(observations)
                if item.get('source') == 'tool_result' and item.get('kind') in {'checks', 'ci'}]
    receipts.extend(_child_receipts(completed_children, chars))
    receipts.extend(_message_receipts(messages, limit, chars))
    return list(dict.fromkeys(receipts))[:limit]


def _observation_receipt(item, chars):
    """One attributed check or CI observation, including its skip details."""
    receipt = (f"Observed {item['kind']} {item.get('tool')} ({item.get('callId')}): "
               f"{item.get('status')}; {item.get('summary')}; "
               f"revision={item.get('revision') or 'unverified'}")
    if 'exitCode' in item:
        receipt += f"; exit_code={item['exitCode']}"
    if 'skipCount' in item:
        receipt += f"; skipped={item['skipCount']}; unknown_skip_reasons={item.get('unknownSkipReasons', 0)}"
    receipt += f" [sha256={item.get('outputSha256')}]"
    if item.get('outputArtifact'):
        receipt += f" Full output: {item['outputArtifact']}"
    if item.get('skippedTests'):
        receipt += '; skip details=' + json.dumps(item['skippedTests'], ensure_ascii=False)
    return scrub(receipt)[:chars]


def _message_receipts(messages, limit, chars):
    """Transcript tool messages that are not this agent's own receipts."""
    receipts = []
    names = _tool_name_index(messages)
    for message in [m for m in messages if m.get('role') == 'tool'][-limit:]:
        name = names.get(message.get('tool_call_id'), 'tool')
        if _is_own_receipt(name):
            continue
        receipt = _tool_receipt(message, name, chars)
        if receipt:
            receipts.append(receipt)
    return receipts

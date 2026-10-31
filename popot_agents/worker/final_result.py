"""Validate final reports and retain bounded receipts when formatting fails."""
import json

from popot_agents.runtime_config import RUNTIME
from .turn_memory import scrub


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


def parse_result(content, finish_reason=None):
    if finish_reason not in (None, 'stop'):
        raise ValueError('truncated_result' if finish_reason == 'length' else 'unexpected_finish_reason')
    if not isinstance(content, str) or not content.strip():
        raise ValueError('empty_content')
    text = content.strip()
    if text.startswith(('```json\n', '```\n')) and text.endswith('\n```'):
        text = text.split('\n', 1)[1].rsplit('\n', 1)[0]
    try:
        report = json.loads(text)
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
    return report


def retained_receipts(messages, completed_children):
    """Receipts are attributed observations, never proof of whole-task completion."""
    limit = RUNTIME['memory']['tool_evidence_count']
    chars = RUNTIME['memory']['tool_evidence_chars']
    receipts = []
    for child in completed_children.values():
        label = f"Child {child['role']} (sessionId={child['sessionId']}) reported: "
        receipts.append(label + scrub(child['answer'])[:chars])
        for item in child.get('outcome', {}).get('evidence', []):
            if isinstance(item, str) and item.strip():
                receipts.append(label + scrub(item)[:chars])
    names = {call.get('id'): call.get('function', {}).get('name', 'tool')
             for m in messages for call in m.get('tool_calls', [])}
    recent = [m for m in messages if m.get('role') == 'tool'][-limit:]
    for m in recent:
        call_id = m.get('tool_call_id')
        name = names.get(call_id, 'tool')
        if name in {'delegate_task', 'review_checkpoint'} or name.startswith('mcp__cognee__'):
            continue
        content = m.get('content')
        if isinstance(content, str) and content.strip():
            receipts.append(f'Tool receipt {name} ({call_id}); outcome requires interpretation: '
                            + scrub(content)[:chars])
    return list(dict.fromkeys(receipts))[:limit]

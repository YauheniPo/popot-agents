"""Compact old tool payloads only after preserving their exact contents."""
import json
import re
import hashlib


def compact_tool_results(messages, save_output, *, budget_chars, recent_results, receipts=()):
    tools = [m for m in messages if m.get('role') == 'tool']
    total = sum(len(m.get('content', '')) for m in tools)
    if total <= budget_chars:
        return 0
    names = {c.get('id'): c.get('function', {}).get('name')
             for m in messages for c in m.get('tool_calls', [])}
    observations = {r['callId']:r for r in receipts}
    changed = 0
    for message in tools[:max(0, len(tools)-recent_results)] if recent_results else tools:
        original = message.get('content', '')
        if total <= budget_chars:
            break
        if len(original) <= 2000 or names.get(message.get('tool_call_id')) in {'delegate_task', 'review_checkpoint'}:
            continue
        try:
            data = json.loads(original)
        except (ValueError, TypeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if data.get('context_compacted') is True:
            continue
        if data.get('sessionId') or data.get('pending'):
            continue
        # Keep machine-readable failure/uncertainty flags, never turn an
        # interrupted write or pending delegation into a successful receipt.
        result = {k: data[k] for k in ('isError', 'outcome_unknown', 'pending', 'resumable', 'exit_code') if k in data}
        if isinstance(data.get('error'), str):
            result['error'] = data['error'][:500]
            if data['error'].startswith('command timed out'):
                result['outcome_unknown'] = True
        exit_code = re.match(r'exit_code=(-?\d+)\n', original)
        if exit_code:
            result['exit_code'] = int(exit_code[1])
            if int(exit_code[1]) != 0:
                result['isError'] = True
        receipt = observations.get(message.get('tool_call_id'))
        if receipt and receipt.get('outputSha256') == hashlib.sha256(original.encode()).hexdigest():
            result['observation'] = {k:v for k,v in receipt.items() if k != 'command'}
        result.update(context_compacted=True, original_chars=len(original),
            outputArtifact=save_output(original), preview=original[:128]+'\n…\n'+original[-128:],
            notice='Saved tool observation, not completion evidence. Read the full artifact with read_tool_output before relying on omitted details.')
        replacement = json.dumps(result, ensure_ascii=False)
        message['content'] = replacement
        total -= len(original)-len(replacement)
        changed += 1
    return changed

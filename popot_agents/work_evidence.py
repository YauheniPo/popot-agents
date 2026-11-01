"""Bounded observations of actual tool results, distinct from task acceptance."""
import hashlib
import json
import re
from datetime import datetime, timezone

from popot_agents.progress import redact_text

MAX_RECEIPTS = 16
_SHA = r'[a-fA-F0-9]{40}'


def observe_tool_result(name, arguments, output, call_id):
    if name in {'delegate_task', 'review_checkpoint', 'read_tool_output'}:
        return None
    text = redact_text(output)
    exit_match = re.match(r'exit_code=(-?\d+)\n', text)
    body = text[exit_match.end():] if exit_match else text
    try:
        data, _ = json.JSONDecoder().raw_decode(body.lstrip())
    except (ValueError, TypeError):
        data = {}
    data = data if isinstance(data, dict) else {}
    command = arguments.get('command', '') if name == 'bash' else ''
    command = command if isinstance(command, str) else ''
    exit_code = int(exit_match[1]) if exit_match else None
    error = bool(data.get('isError') or data.get('error') or exit_code not in (None, 0))
    status, kind, summary = 'observed', None, ''
    revision = None
    if name == 'bash':
        checks = re.search(r'Ran \d+ tests?(?: in [\d.]+s)?|\d+ passed(?:, \d+ failed)?|FAILED \([^\n)]{0,80}\)|# pass \d+', text)
        if checks and re.search(r'\b(?:pytest|unittest)\b|\b(?:npm|pnpm|yarn)\s+(?:run\s+)?test\b|\bnode\b[^\n]*--test', command):
            kind, summary = 'checks', checks[0].strip()
            failed_checks = re.search(r'\b[1-9]\d* (?:failed|errors?)\b|FAILED \(|(?m:^EXIT_CODE=[1-9]\d*\s*$)', text)
            if error or failed_checks:
                status = 'failed'
            elif exit_code == 0 and re.search(r'(?m)^OK(?:\s|$)|\d+ passed|# pass \d+', text):
                status = 'passed'
        elif exit_code == 0 and re.search(r'\bgit\b[^\n;|&]*\bcommit\b', command) and re.search(r'(?m)^\[[^\]\n]+ [a-f0-9]{7,40}\]', text):
            kind, summary = 'commit', 'Git commit returned successfully; publication is separate.'
        elif exit_code == 0 and re.search(r'\bgit\b[^\n;|&]*\bpush\b', command) and re.search(r'(?m)^\s+\S+\s+\S+\s+->\s+\S+', text):
            kind, summary = 'publication', 'Git push returned a remote-ref update; verify the remote head and CI.'
        if re.search(r'git\s+rev-parse\s+HEAD', command):
            match = re.search(r'(?m)^('+_SHA+r')\s*$', text)
            revision = match[1].lower() if match else None
            if revision and kind is None:
                kind, summary = 'revision', 'Git HEAD read-back observed.'
    if 'github' in name or name == 'bash' and exit_code == 0:
        head = data.get('head', {})
        sha = head.get('sha') if isinstance(head, dict) else head
        sha = sha or data.get('headRefOid')
        if name.endswith('get_commit'):
            sha = data.get('sha')
        if isinstance(sha, str) and re.fullmatch(_SHA, sha):
            revision = sha.lower()
            kind, summary = kind or 'revision', summary or 'Revision read-back observed in tool result.'
        if re.search(r'"conclusion"\s*:', text):
            kind, summary = 'ci', 'GitHub check-run results observed; inspect individual conclusions.'
            records = data.get('check_runs', data.get('statusCheckRollup', []))
            records = [r for r in records if isinstance(r, dict)] if isinstance(records, list) else []
            failed = [str(r.get('name','check'))[:80] for r in records
                      if str(r.get('conclusion','')).lower() in {'failure','error','timed_out','action_required','cancelled'}]
            if failed:
                status, summary = 'failed', 'CI failures: ' + ', '.join(failed[:4])
            elif records and all(str(r.get('conclusion','')).lower() in {'success','neutral','skipped'} for r in records):
                status, summary = 'passed', 'All returned CI checks finished successfully, neutral or skipped.'
            elif records:
                summary = 'CI pending: ' + ', '.join(str(r.get('name','check'))[:80] for r in records
                    if not r.get('conclusion'))[:250]
    if error:
        kind = kind or 'blocker'
        timed_out = name == 'bash' and str(data.get('error', '')).startswith('command timed out')
        status = 'unknown' if data.get('outcome_unknown') or timed_out else 'failed'
        summary = (f'exit_code={exit_code}; {summary}' if kind == 'checks' else
                   ('Shell command timed out; reconcile effects using the saved tool output.' if timed_out
                    else 'Shell execution failed; inspect the saved tool output.')
                   if name == 'bash' and 'partial output' in str(data.get('error', ''))
                   else str(data.get('error') or f'Tool returned exit code {exit_code}')[:320])
    if kind is None:
        return None
    receipt = {'callId':str(call_id)[:128], 'tool':name[:160], 'kind':kind, 'status':status,
        'source':'tool_result', 'observedAt':datetime.now(timezone.utc).isoformat(),
        'outputSha256':hashlib.sha256(output.encode()).hexdigest(),
        'summary':summary[:320]}
    if revision:
        receipt['revision'] = revision
    if exit_code is not None:
        receipt['exitCode'] = exit_code
    if command:
        receipt['command'] = redact_text(command)[:512]
        receipt['commandTruncated'] = len(command) > 512
        receipt['commandSha256'] = hashlib.sha256(command.encode()).hexdigest()
    if status == 'unknown':
        receipt['outcomeUnknown'] = True
    artifact = re.search(r'Full output: (\.tool-results/[a-f0-9]{32}\.txt)', text)
    if artifact:
        receipt['outputArtifact'] = artifact[1]
    refs = re.findall(r'https://github\.com/[\w.-]+/[\w.-]+/(?:commit/[a-f0-9]{40}|pull/\d+|actions/runs/\d+(?:/job/\d+)?)', text)
    if refs:
        receipt['references'] = list(dict.fromkeys(refs))[:4]
    return receipt


def retain_evidence(existing, receipt):
    # The call ID deduplicates progress forwarded through several coordinators.
    key = (receipt['callId'], receipt['observedAt'])
    return [r for r in existing if (r.get('callId'), r.get('observedAt')) != key][-MAX_RECEIPTS+1:] + [receipt]


def evidence_event(message):
    """Accept only the small receipt contract, never arbitrary trace payloads."""
    try:
        event = json.loads(message)
    except (ValueError, TypeError):
        return None
    value = event.get('receipt') if isinstance(event, dict) and event.get('event') == 'work_evidence' else None
    if not isinstance(value, dict) or value.get('source') != 'tool_result':
        return None
    if value.get('kind') not in {'commit','publication','revision','checks','ci','blocker'} or value.get('status') not in {'observed','passed','failed','unknown'}:
        return None
    result = {}
    for key, size in (('callId',128),('tool',160),('kind',32),('status',32),('source',32),
                      ('summary',320),('observedAt',64),('command',512),('commandSha256',64),('outputSha256',64)):
        if isinstance(value.get(key), str):
            result[key] = redact_text(value[key])[:size]
    if not all(result.get(k) for k in ('callId','tool','observedAt','summary')):
        return None
    if isinstance(value.get('revision'), str) and re.fullmatch(_SHA, value['revision']):
        result['revision'] = value['revision'].lower()
    if type(value.get('exitCode')) is int:
        result['exitCode'] = value['exitCode']
    if value.get('outcomeUnknown') is True:
        result['outcomeUnknown'] = True
    if value.get('commandTruncated') is True:
        result['commandTruncated'] = True
    if isinstance(value.get('outputArtifact'), str) and re.fullmatch(r'\.tool-results/[a-f0-9]{32}\.txt', value['outputArtifact']):
        result['outputArtifact'] = value['outputArtifact']
    if isinstance(value.get('references'), list):
        result['references'] = [ref for ref in value['references'][:4] if isinstance(ref, str)
            and len(ref) <= 400 and re.fullmatch(r'https://github\.com/[\w./-]+', ref)]
    return result


def reusable_checks(receipts, revision):
    """No revision or a different revision is never a reusable test result."""
    if not isinstance(revision, str) or not re.fullmatch(_SHA, revision):
        return []
    return [r for r in receipts if isinstance(r, dict) and r.get('source') == 'tool_result'
            and r.get('kind') == 'checks' and r.get('status') == 'passed'
            and r.get('revision') == revision.lower() and r.get('exitCode') == 0]

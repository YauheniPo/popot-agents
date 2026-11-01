"""Bounded, durable worker reports for MCP observers; no live worker calls."""
import json
import re

from popot_agents.progress import redact_text

_MAX_BYTES = 16384
_REFERENCE = re.compile(
    r'https://github\.com/[\w.-]+/[\w.-]+/(?P<kind>commit/[a-f0-9]{40}|pull/\d+|actions/runs/\d+(?:/job/\d+)?)',
    re.I)
_COMMIT = re.compile(r'(?:\b(?:commit(?:\s+sha)?|head|revision)\s*[:=]?\s+|^=== commit ===\s*)([a-f0-9]{40})\b', re.I | re.M)


def _report(saved):
    config = saved.get('roleConfig') or {}
    child, workflow = config.get('_ax_delegation', {}), config.get('_workflow', {})
    outcome = child.get('outcome') or workflow.get('outcome')
    recorded_at = child.get('outcomeRecordedAt') or workflow.get('outcomeRecordedAt')
    report = {'sessionId': saved['sessionId'], 'role': saved.get('role'),
              'parentSessionId': child.get('parent_session_id'),
              'executionState': child.get('state') or workflow.get('state', 'unknown'),
              'reportedAt': recorded_at or saved.get('updatedAt'),
              'timeSource': 'outcome_recorded' if recorded_at else 'session_updated',
              'coordinatorTurns': (child.get('workflow') or workflow).get('turns'),
              'outcome': None, 'references': [], 'commits': [], 'truncated': False}
    observations = config.get('_work_evidence', [])
    report['milestones'] = [{key: value for key, value in receipt.items() if key not in {'command'}}
                             for receipt in observations[-4:]]
    report['truncated'] |= len(observations) > 4
    report['lastObservedAt'] = observations[-1].get('observedAt') if observations else None
    for receipt in report['milestones']:
        if receipt.get('revision') and receipt['revision'] not in report['commits']:
            report['commits'].append(receipt['revision'])
        for url in receipt.get('references', []):
            if len(report['references']) < 6:
                report['references'].append({'type':'tool_reference','url':url})
            else:
                report['truncated'] = True
    if not isinstance(outcome, dict):
        return report
    labels = {key: redact_text(str(outcome[key])) for key in ('status', 'reason') if key in outcome}
    report['outcome'] = {key: text[:200] for key, text in labels.items()}
    report['truncated'] |= any(len(text) > 200 for text in labels.values())
    for key, count in (('evidence', 8), ('remaining_work', 2), ('errors', 2), ('warnings', 2)):
        values = outcome.get(key, [])
        values = [item for item in values if isinstance(item, str)] if isinstance(values, list) else []
        texts = [redact_text(item) for item in values[:count]]
        report['outcome'][key] = [text[:240] for text in texts]
        report['truncated'] |= len(values) > count or any(len(text) > 240 for text in texts)
    # Links remain complete even when the surrounding evidence is shortened.
    # Their presence is a reported reference, never proof of publication/CI success.
    sources = list(outcome.get('evidence') or [])[:16]
    if isinstance(outcome.get('last_partial_answer'), str):
        sources.append(outcome['last_partial_answer'])
    seen = {ref['url'] for ref in report['references']}
    commits = set(report['commits'])
    for text in sources:
        if not isinstance(text, str):
            continue
        report['truncated'] |= len(text) > 12000
        text = redact_text(text[:12000])
        for match in _REFERENCE.finditer(text):
            url, kind = match.group(), match.group('kind').lower()
            if url in seen:
                continue
            seen.add(url)
            if len(report['references']) == 6:
                report['truncated'] = True
                continue
            reference = {'type': 'commit' if kind.startswith('commit/') else
                         'pull_request' if kind.startswith('pull/') else 'ci_run', 'url': url}
            if reference['type'] == 'commit':
                reference['sha'] = kind.split('/')[1]
                commits.add(reference['sha'])
            report['references'].append(reference)
        commits.update(match.group(1).lower() for match in _COMMIT.finditer(text))
    report['commits'] = sorted(commits)[:6]
    report['truncated'] |= len(commits) > 6
    failure = child.get('failure')
    if isinstance(failure, dict):
        report['failure'] = {key: redact_text(str(failure[key]))[:160]
                             for key in ('code', 'stage', 'error_type') if key in failure}
    return report


def build_work_status(saved, chats):
    root_id = ((saved.get('roleConfig') or {}).get('_ax_delegation') or {}).get('root_session_id', saved['sessionId'])
    related = {item['sessionId']: item for item in chats if item['sessionId'] == root_id or
               ((item.get('roleConfig') or {}).get('_ax_delegation') or {}).get('root_session_id') == root_id}
    related[saved['sessionId']] = saved
    ordered = sorted(related.values(), key=lambda item: (
        item.get('updatedAt', ''), item['sessionId']), reverse=True)
    ordered.sort(key=lambda item: (item['sessionId'] != saved['sessionId'], item['sessionId'] != root_id))
    result = {'rootSessionId': root_id, 'source': 'stored_worker_reports',
              'taskCount': len(ordered), 'tasks': [], 'truncated': len(ordered) > 8}
    for item in ordered[:8]:
        report = _report(item)
        result['tasks'].append(report)
        while report['milestones'] and len(json.dumps(result, ensure_ascii=False).encode()) > _MAX_BYTES:
            report['milestones'].pop(0)
            report['truncated'] = True
        if len(json.dumps(result, ensure_ascii=False).encode()) > _MAX_BYTES:
            result['tasks'].pop()
            result['truncated'] = True
            continue
        result['truncated'] |= report['truncated']
    return result

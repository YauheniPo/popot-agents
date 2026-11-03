"""Bind reported repository verification to an observed immutable revision."""
import re
from datetime import datetime, timezone

_PULL_REQUEST = re.compile(r'github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)')
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _moment(value):
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _fresh(receipt, reported):
    observed = _moment(receipt.get('observedAt'))
    if observed is None:
        return False
    return reported is None or observed > reported


def requested_pull_request(messages):
    """The single pull request linked by a user request, or None when that is ambiguous."""
    targets = set()
    for message in messages or ():
        if not isinstance(message, dict) or message.get('role') != 'user':
            continue
        content = message.get('content')
        if isinstance(content, str):
            targets.update((owner.lower(), repo.lower(), int(number))
                           for owner, repo, number in _PULL_REQUEST.findall(content))
    if len(targets) != 1:
        return None
    owner, repo, number = targets.pop()
    return {'repository': f'{owner}/{repo}', 'pullRequest': number}


def observed_revision(report, receipts, target=None):
    observed = [r['revision'] for r in receipts if re.fullmatch(r'[a-f0-9]{40}', r.get('revision',''))
                and r.get('status') not in {'failed','unknown'}]
    declared = (report or {}).get('revision')
    if declared:
        return declared if declared in observed else None
    if target:
        return target if target in observed else None
    return observed[-1] if observed else None


def acceptance_revision(children, receipts, target=None):
    implementations = [c for c in children if c.get('role') in {'backend_engineer','frontend_engineer'}]
    implementations.sort(key=lambda c:c.get('reportedAt',''))
    latest = implementations[-1] if implementations else {}
    revision = latest.get('outcome',{}).get('revision')
    reported = _moment(latest.get('reportedAt'))
    # A newer parent read-back can establish that the PR head changed again, but only
    # for the pull request the user asked about and only at its latest observed head.
    fresh = [r for r in receipts
             if r.get('revision') and r.get('revisionSource') == 'pr_head'
             and r.get('status') not in {'failed','unknown'}
             and _fresh(r, reported)
             and (target is None or (str(r.get('repository','')).lower() == target['repository']
                                     and r.get('pullRequest') == target['pullRequest']))]
    newest = max(fresh, key=lambda r: _moment(r.get('observedAt')) or _EPOCH, default=None)
    return newest['revision'] if newest else revision


def verified_roles(children, target):
    return {c.get('role') for c in children
        if c.get('outcome',{}).get('status') == 'completed' and c.get('outcome',{}).get('evidence')
        and (target is None or c.get('outcome',{}).get('revision') == target)}

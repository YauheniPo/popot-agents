"""Bind reported repository verification to an observed immutable revision."""
import re


def observed_revision(report, receipts, target=None):
    observed = [r['revision'] for r in receipts if re.fullmatch(r'[a-f0-9]{40}', r.get('revision',''))
                and r.get('status') not in {'failed','unknown'}]
    declared = (report or {}).get('revision')
    if declared:
        return declared if declared in observed else None
    if target:
        return target if target in observed else None
    return observed[-1] if observed else None


def acceptance_revision(children, receipts):
    implementations = [c for c in children if c.get('role') in {'backend_engineer','frontend_engineer'}]
    implementations.sort(key=lambda c:c.get('reportedAt',''))
    latest = implementations[-1] if implementations else {}
    target = latest.get('outcome',{}).get('revision')
    # A newer parent read-back can establish that the PR head changed again.
    fresh = [r for r in receipts if r.get('revision') and r.get('revisionSource') == 'pr_head'
             and r.get('status') not in {'failed','unknown'}
             and r.get('observedAt','') > latest.get('reportedAt','')]
    return fresh[-1]['revision'] if fresh else target


def verified_roles(children, target):
    return {c.get('role') for c in children
        if c.get('outcome',{}).get('status') == 'completed' and c.get('outcome',{}).get('evidence')
        and (target is None or c.get('outcome',{}).get('revision') == target)}

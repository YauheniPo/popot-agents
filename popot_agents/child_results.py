"""Reconcile child reports while preserving unrelated assignments and evidence."""

import json
import re
from copy import deepcopy

from popot_agents.progress import redact_text
from popot_agents.work_evidence import child_resolution, MAX_RECEIPTS


def completed_answer(output: str, role: str, *, session_id: str | None = None) -> str:
    """Validate the untrusted response envelope returned by delegate_task."""
    try:
        result = json.loads(output)
    except (TypeError, ValueError):
        raise RuntimeError(f'delegation to {role} returned invalid JSON') from None
    if not isinstance(result, dict):
        raise RuntimeError(f'delegation to {role} returned an invalid result')
    session = result.get('sessionId')
    suffix = f' (sessionId={session})' if isinstance(session, str) and re.fullmatch(r'[0-9a-f]{16}', session) else ''
    answer = result.get('answer')
    outcome = result.get('outcome')
    if (result.get('isError') or result.get('error') or result.get('state') != 'TASK_STATE_COMPLETED'
            or (session_id is not None and session != session_id)
            or (outcome is not None and (not isinstance(outcome, dict) or outcome.get('status') != 'completed'
                                        or outcome.get('errors') or outcome.get('remaining_work')))
            or result.get('role') != role or result.get('truncated')
            or not isinstance(answer, str) or not answer.strip() or not suffix):
        diagnostic = '; '.join(redact_text(result[key])[:500] for key in ('state', 'error')
                               if isinstance(result.get(key), str) and result[key])
        raise RuntimeError(f'delegation to {role} did not return a complete successful answer{suffix}; '
                           + (diagnostic + '; ' if diagnostic else '')
                           + 'inspect the child result before retrying external actions')
    return f'{role} · sessionId: {session}\n\n{answer.strip()}'


class ChildResults:
    """Own the failed/completed/resolved portion of a harness checkpoint.

    Callers may read the collections for routing and acceptance decisions;
    changes go through record_response or deliver_reports. Authorization,
    waiting and worker lifecycle remain the caller's responsibility.
    """

    def __init__(self, checkpoint=None):
        checkpoint = checkpoint or {}
        self.failed = {tuple(key): deepcopy(value)
                       for key, value in checkpoint.get('failed_delegations', [])}
        self.completed = deepcopy(checkpoint.get('completed_delegations', {}))
        self.resolved = deepcopy(checkpoint.get('resolved_delegations', []))

    def snapshot(self):
        return deepcopy({
            'failed_delegations': [[list(key), value] for key, value in self.failed.items()],
            'completed_delegations': self.completed,
            'resolved_delegations': self.resolved,
        })

    def _resolve(self, previous, child, revision, *, require_evidence=True):
        resolution = child_resolution(previous, child, revision, require_evidence=require_evidence)
        self.resolved.append(resolution)
        self.resolved[:] = self.resolved[-MAX_RECEIPTS:]
        return resolution

    @staticmethod
    def _rejected_outcome(outcome, error):
        return {**outcome, 'status': 'failed', 'reason': 'child_reconciliation_invalid',
                'errors': [error],
                'remaining_work': ['Verify the required revision and remaining deliverables']}

    def record_response(self, key, output, *, session_id=None, revision=None, require_evidence=True):
        """Apply a tool response; return the accepted child and optional resolution."""
        old_failure = self.failed.get(key) or next((value for value in self.failed.values()
            if session_id and value.get('session_id') == session_id and value.get('role') == key[0]), None)
        resolution = None
        try:
            completed_answer(output, key[0], session_id=session_id)
            child = json.loads(output)
            if old_failure:
                resolution = self._resolve(old_failure.get('previous_failure', old_failure),
                    child, revision, require_evidence=require_evidence)
        except RuntimeError as exc:
            try:
                child = json.loads(output)
            except (TypeError, ValueError):
                child = {}
            child_session = session_id or (child.get('sessionId') if isinstance(child, dict) else None)
            previous = {}
            if session_id:
                for previous_key, value in list(self.failed.items()):
                    if previous_key[0] == key[0] and value.get('session_id') == child_session:
                        previous = self.failed.pop(previous_key)
            failure = {**previous, 'pending': isinstance(child, dict) and child.get('pending') is True,
                'error': str(exc), 'role': key[0], 'session_id': child_session,
                'status': child.get('outcome', {}).get('status') if isinstance(child, dict)
                    and isinstance(child.get('outcome'), dict) else None,
                'resumable': isinstance(child, dict) and child.get('resumable') is True}
            if old_failure and (not old_failure.get('pending') or old_failure.get('previous_failure')):
                failure['previous_failure'] = old_failure.get('previous_failure') or {
                    name: old_failure.get(name) for name in ('role', 'session_id', 'outcome')}
            if revision:
                failure['target_revision'] = revision
            if isinstance(child, dict) and child.get('role') == key[0] \
                    and child.get('sessionId') == child_session and isinstance(child.get('answer'), str) \
                    and child['answer'].strip():
                failure['answer'] = child['answer']
                failure['outcome'] = child['outcome'] if isinstance(child.get('outcome'), dict) else {}
            if old_failure and failure.get('outcome', {}).get('status') == 'completed':
                failure.update(status='failed', resumable=False)
                failure['outcome'] = self._rejected_outcome(failure['outcome'], str(exc))
            self.failed[key] = failure
            return None, None
        self.completed[child['sessionId']] = child
        self.failed.pop(key, None)
        if session_id:
            for previous_key, value in list(self.failed.items()):
                if previous_key[0] == key[0] and value.get('session_id') == session_id:
                    del self.failed[previous_key]
        return child, resolution

    def deliver_reports(self, receipts):
        """Apply terminal durable reports after the caller checked ownership.

        Durable reports already have a server-built envelope. Evidence/revision
        checks apply to recovery of a previous failure, unlike strict tool replies.
        Return the reconciled reports for delivery to the parent's conversation.
        """
        receipts = deepcopy(receipts)
        for key, value in self.failed.items():
            receipt = receipts.get(value.get('session_id'))
            if receipt is not None and key[0] != receipt['role']:
                raise ValueError('Pending child role mismatch')
        failed = {}
        for key, value in self.failed.items():
            receipt = receipts.get(value.get('session_id'))
            if receipt is None:
                failed[key] = value
                continue
            if receipt['outcome'].get('status') == 'completed' and value.get('previous_failure'):
                try:
                    self._resolve(value['previous_failure'], receipt, value.get('target_revision'))
                except RuntimeError as exc:
                    receipt.update(resumable=False, error=str(exc))
                    receipt['outcome'] = self._rejected_outcome(receipt['outcome'], str(exc))
            if receipt['outcome'].get('status') != 'completed':
                failed[key] = {**value, 'pending': False, 'answer': receipt['answer'],
                    'status': receipt['outcome'].get('status'), 'outcome': receipt['outcome'],
                    'resumable': receipt.get('resumable', False),
                    'error': receipt.get('error', 'Child returned incomplete work')}
        self.failed.clear()
        self.failed.update(failed)
        for sid, receipt in receipts.items():
            if receipt['outcome'].get('status') == 'completed':
                self.completed[sid] = receipt
        return deepcopy(receipts)

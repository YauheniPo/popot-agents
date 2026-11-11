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
        old_failure = self._prior_failure(key, session_id)
        resolution = None
        try:
            completed_answer(output, key[0], session_id=session_id)
            child = json.loads(output)
            if old_failure:
                resolution = self._resolve(old_failure.get('previous_failure', old_failure),
                    child, revision, require_evidence=require_evidence)
        except RuntimeError as exc:
            self._record_failure(key, output, session_id, revision, old_failure, exc)
            return None, None
        self.completed[child['sessionId']] = child
        self._drop_failures(key[0], key, session_id)
        return child, resolution

    def _prior_failure(self, key, session_id):
        """The failure record this response continues, if any."""
        if key in self.failed:
            return self.failed[key]
        if not session_id:
            return None
        return next((value for value in self.failed.values()
                     if value.get('session_id') == session_id and value.get('role') == key[0]), None)

    def _drop_failures(self, role, key, session_id):
        """Forget the failures this accepted response supersedes."""
        self.failed.pop(key, None)
        if not session_id:
            return
        stale = [previous_key for previous_key, value in self.failed.items()
                 if previous_key[0] == role and value.get('session_id') == session_id]
        for previous_key in stale:
            del self.failed[previous_key]

    def _pop_failure(self, role, session_id):
        """Remove and return the failure record for this role and session."""
        match = next((previous_key for previous_key, value in self.failed.items()
                      if previous_key[0] == role and value.get('session_id') == session_id), None)
        return self.failed.pop(match) if match is not None else {}

    def _record_failure(self, key, output, session_id, revision, old_failure, exc):
        """Store the failure envelope for an unavailable or invalid response."""
        child = _parsed_child(output)
        child_session = session_id or (child.get('sessionId') if isinstance(child, dict) else None)
        previous = self._pop_failure(key[0], child_session) if session_id else {}
        failure = _failure_envelope(key[0], child, child_session, exc, previous)
        if old_failure and (not old_failure.get('pending') or old_failure.get('previous_failure')):
            failure['previous_failure'] = old_failure.get('previous_failure') or {
                name: old_failure.get(name) for name in ('role', 'session_id', 'outcome')}
        if revision:
            failure['target_revision'] = revision
        if _own_answer(child, key[0], child_session):
            # Keep a rejected completed answer so a later report cannot hide it.
            failure['answer'] = child['answer']
            failure['outcome'] = child['outcome'] if isinstance(child.get('outcome'), dict) else {}
        if old_failure and failure.get('outcome', {}).get('status') == 'completed':
            failure.update(status='failed', resumable=False)
            failure['outcome'] = self._rejected_outcome(failure['outcome'], str(exc))
        self.failed[key] = failure

    def deliver_reports(self, receipts):
        """Apply terminal durable reports after the caller checked ownership.

        Durable reports already have a server-built envelope. Evidence/revision
        checks apply to recovery of a previous failure, unlike strict tool replies.
        Return the reconciled reports for delivery to the parent's conversation.
        """
        receipts = deepcopy(receipts)
        self._reject_role_mismatch(receipts)
        failed = {}
        for key, value in self.failed.items():
            receipt = receipts.get(value.get('session_id'))
            if receipt is None:
                failed[key] = value
                continue
            failed.update(self._reconciled_failure(key, value, receipt))
        self.failed.clear()
        self.failed.update(failed)
        for session_id, receipt in receipts.items():
            if receipt['outcome'].get('status') == 'completed':
                self.completed[session_id] = receipt
        return deepcopy(receipts)

    def _reject_role_mismatch(self, receipts):
        """A pending child's durable report must carry that child's own role."""
        for key, value in self.failed.items():
            receipt = receipts.get(value.get('session_id'))
            if receipt is not None and key[0] != receipt['role']:
                raise ValueError('Pending child role mismatch')

    def _reconciled_failure(self, key, value, receipt):
        """Failure records retained after a durable report arrived."""
        if receipt['outcome'].get('status') == 'completed' and value.get('previous_failure'):
            try:
                self._resolve(value['previous_failure'], receipt, value.get('target_revision'))
            except RuntimeError as exc:
                receipt.update(resumable=False, error=str(exc))
                receipt['outcome'] = self._rejected_outcome(receipt['outcome'], str(exc))
        if receipt['outcome'].get('status') == 'completed':
            return {}
        return {key: {**value, 'pending': False, 'answer': receipt['answer'],
            'status': receipt['outcome'].get('status'), 'outcome': receipt['outcome'],
            'resumable': receipt.get('resumable', False),
            'error': receipt.get('error', 'Child returned incomplete work')}}

def _parsed_child(output):
    """The decoded response payload, or an empty mapping when it cannot be parsed."""
    try:
        return json.loads(output)
    except (TypeError, ValueError):
        return {}


def _child_outcome_status(child):
    """The reported outcome status of a parsed response, if any."""
    outcome = child.get('outcome') if isinstance(child, dict) else None
    return outcome.get('status') if isinstance(outcome, dict) else None


def _failure_envelope(role, child, session_id, exc, previous):
    """The stored failure record for one unusable response."""
    known = child if isinstance(child, dict) else {}
    return {**previous, 'pending': known.get('pending') is True, 'error': str(exc),
            'role': role, 'session_id': session_id, 'status': _child_outcome_status(child),
            'resumable': known.get('resumable') is True}


def _own_answer(child, role, session_id):
    """Whether the response carries this child's own non-empty answer."""
    return bool(isinstance(child, dict) and child.get('role') == role
                and child.get('sessionId') == session_id
                and isinstance(child.get('answer'), str) and child['answer'].strip())

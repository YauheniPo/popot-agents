"""Child reports reconcile one assignment without erasing unrelated work."""
import json
from copy import deepcopy

import pytest

from popot_agents.child_results import ChildResults


ROLE = 'qa_engineer'
SESSION = 'a' * 16
OTHER_SESSION = 'b' * 16
REVISION = 'c' * 40
KEY = (ROLE, 'Verify the artifact')


def failure(session=SESSION, *, role=ROLE):
    return {'role': role, 'session_id': session, 'status': 'blocked',
            'pending': False, 'resumable': True, 'error': 'Read-back remains',
            'outcome': {'status': 'blocked', 'reason': 'workflow_turn_limit',
                        'evidence': ['artifact already written'],
                        'remaining_work': ['Read back the artifact'], 'errors': []}}


def pending_recovery(session=SESSION):
    return {**failure(session), 'pending': True, 'status': 'partial',
            'target_revision': REVISION, 'previous_failure': failure(session)}


def report(session=SESSION, *, role=ROLE):
    return {'sessionId': session, 'role': role, 'state': 'TASK_STATE_COMPLETED',
            'answer': 'Artifact verified', 'outcome': {
                'status': 'completed', 'revision': REVISION,
                'evidence': ['read-back verified'], 'remaining_work': [], 'errors': []}}


def checkpoint(value=None):
    return {'failed_delegations': [[list(KEY), value if value is not None else failure()]],
            'completed_delegations': {}, 'resolved_delegations': []}


def test_response_recovery_resolves_its_session_when_assignment_text_changes():
    saved = checkpoint()
    sibling_key = (ROLE, 'Verify another artifact')
    sibling = failure(OTHER_SESSION)
    saved['failed_delegations'].append([list(sibling_key), sibling])
    children = ChildResults(saved)

    completed, resolution = children.record_response(
        (ROLE, 'Finish read-back'), json.dumps(report()),
        session_id=SESSION, revision=REVISION)

    assert completed == report()
    assert children.failed == {sibling_key: sibling}
    assert children.completed == {SESSION: completed}
    assert children.resolved == [resolution]
    assert resolution['session_id'] == SESSION
    assert resolution['resolved_by'] == SESSION
    assert resolution['previous_reason'] == 'workflow_turn_limit'
    assert resolution['revision'] == REVISION
    assert resolution['evidence'] == ['read-back verified']


def test_retry_of_same_assignment_can_resolve_a_replacement_session():
    saved = checkpoint()
    unrelated_key = ('backend_engineer', KEY[1])
    unrelated = failure('d' * 16, role='backend_engineer')
    saved['failed_delegations'].append([list(unrelated_key), unrelated])
    children = ChildResults(saved)

    completed, resolution = children.record_response(
        KEY, json.dumps(report(OTHER_SESSION)), revision=REVISION)

    assert completed['sessionId'] == OTHER_SESSION
    assert resolution['session_id'] == SESSION
    assert resolution['resolved_by'] == OTHER_SESSION
    assert children.failed == {unrelated_key: unrelated}


def test_pending_recovery_crosses_json_checkpoint_before_durable_delivery():
    children = ChildResults(checkpoint())
    pending = {'sessionId': SESSION, 'role': ROLE, 'state': 'TASK_STATE_WORKING',
               'pending': True, 'outcome': {'status': 'partial', 'evidence': [],
                                          'remaining_work': ['working'], 'errors': []}}
    resumed_key = (ROLE, 'Finish read-back')

    assert children.record_response(resumed_key, json.dumps(pending),
                                    session_id=SESSION, revision=REVISION) == (None, None)
    saved = json.loads(json.dumps(children.snapshot()))
    restored = ChildResults(saved)
    assert restored.failed[resumed_key]['pending'] is True
    assert restored.failed[resumed_key]['previous_failure']['outcome']['reason'] == 'workflow_turn_limit'

    delivered = restored.deliver_reports({SESSION: report()})

    assert delivered == {SESSION: report()}
    assert restored.failed == {}
    assert restored.completed == {SESSION: report()}
    assert restored.resolved[0]['previous_reason'] == 'workflow_turn_limit'
    assert restored.resolved[0]['revision'] == REVISION


@pytest.mark.parametrize('durable', [False, True])
@pytest.mark.parametrize('change', [
    {'revision': 'd' * 40}, {'evidence': []},
    {'errors': ['read-back failed']}, {'remaining_work': ['Read back another file']},
])
def test_recovery_rejects_incomplete_evidence_or_wrong_revision(durable, change):
    children = ChildResults(checkpoint(pending_recovery() if durable else failure()))
    child = report()
    child['outcome'].update(change)

    if durable:
        delivered = children.deliver_reports({SESSION: child})
        assert delivered[SESSION]['outcome']['status'] == 'failed'
        assert delivered[SESSION]['outcome']['reason'] == 'child_reconciliation_invalid'
    else:
        assert children.record_response(KEY, json.dumps(child), session_id=SESSION,
                                        revision=REVISION) == (None, None)

    assert children.failed[KEY]['status'] == 'failed'
    assert children.failed[KEY]['outcome']['reason'] == 'child_reconciliation_invalid'
    assert SESSION not in children.completed
    assert children.resolved == []


@pytest.mark.parametrize('durable', [False, True])
def test_first_completed_report_does_not_require_recovery_evidence(durable):
    child = report()
    child['outcome'] = {'status': 'completed', 'errors': [], 'remaining_work': []}
    children = ChildResults(checkpoint({'role': ROLE, 'session_id': SESSION, 'pending': True})
                            if durable else None)

    if durable:
        assert children.deliver_reports({SESSION: child}) == {SESSION: child}
    else:
        completed, resolution = children.record_response(KEY, json.dumps(child))
        assert completed == child
        assert resolution is None

    assert children.failed == {}
    assert children.completed == {SESSION: child}
    assert children.resolved == []


@pytest.mark.parametrize('require_evidence', [False, True])
def test_legacy_response_policy_explicitly_controls_recovery_evidence(require_evidence):
    children = ChildResults(checkpoint())
    child = report()
    child.pop('outcome')

    completed, resolution = children.record_response(
        KEY, json.dumps(child), require_evidence=require_evidence)

    if require_evidence:
        assert completed is None
        assert resolution is None
        assert KEY in children.failed
    else:
        assert completed == child
        assert resolution['previous_reason'] == 'workflow_turn_limit'
        assert children.failed == {}


@pytest.mark.parametrize('invalid', [
    'not JSON', '[]', json.dumps({**report(), 'role': 'backend_engineer'}),
    json.dumps(report(OTHER_SESSION)), json.dumps({**report(), 'truncated': True}),
    json.dumps({**report(), 'state': 'TASK_STATE_FAILED', 'isError': True}),
])
def test_invalid_response_cannot_complete_recovery_or_erase_other_results(invalid):
    saved = checkpoint()
    sibling = report(OTHER_SESSION)
    saved['completed_delegations'][OTHER_SESSION] = sibling
    children = ChildResults(saved)

    assert children.record_response(KEY, invalid, session_id=SESSION,
                                    revision=REVISION) == (None, None)

    assert children.failed[KEY]['role'] == ROLE
    assert children.failed[KEY]['session_id'] == SESSION
    assert children.completed == {OTHER_SESSION: sibling}
    assert children.resolved == []


def test_durable_role_mismatch_rejects_the_batch_before_changing_state():
    saved = checkpoint({'role': ROLE, 'session_id': SESSION, 'pending': True})
    saved['failed_delegations'].append([[ROLE, 'Verify another artifact'],
        {'role': ROLE, 'session_id': OTHER_SESSION, 'pending': True}])
    children = ChildResults(saved)
    before = children.snapshot()

    reports = {SESSION: report(),
               OTHER_SESSION: report(OTHER_SESSION, role='backend_engineer')}
    with pytest.raises(ValueError, match='Pending child role mismatch'):
        children.deliver_reports(reports)

    assert children.snapshot() == before


def test_constructor_and_snapshot_own_their_nested_state():
    saved = checkpoint()
    saved['completed_delegations'][OTHER_SESSION] = report(OTHER_SESSION)
    saved['resolved_delegations'] = [{'evidence': ['prior check']}]
    expected = deepcopy(saved)
    children = ChildResults(saved)

    saved['failed_delegations'][0][1]['outcome']['evidence'].append('external edit')
    saved['completed_delegations'][OTHER_SESSION]['outcome']['evidence'].clear()
    saved['resolved_delegations'][0]['evidence'].clear()
    assert children.snapshot() == expected

    exported = children.snapshot()
    exported['failed_delegations'][0][1]['outcome']['evidence'].clear()
    exported['completed_delegations'][OTHER_SESSION]['answer'] = 'external edit'
    exported['resolved_delegations'].clear()
    assert children.snapshot() == expected
    assert json.loads(json.dumps(children.snapshot())) == expected


def test_rejected_durable_recovery_does_not_mutate_input_receipts():
    children = ChildResults(checkpoint(pending_recovery()))
    receipt = report()
    receipt['outcome']['revision'] = 'd' * 40
    receipts = {SESSION: receipt}
    original = deepcopy(receipts)

    delivered = children.deliver_reports(receipts)

    assert receipts == original
    assert delivered[SESSION]['outcome']['status'] == 'failed'
    delivered[SESSION]['outcome']['errors'].append('caller edit')
    assert 'caller edit' not in children.failed[KEY]['outcome']['errors']


@pytest.mark.parametrize('durable', [False, True])
def test_resolution_history_keeps_only_the_latest_sixteen(durable):
    saved = checkpoint(pending_recovery() if durable else failure())
    old = [{'session_id': f'previous-{index}', 'evidence': ['checked']} for index in range(16)]
    saved['resolved_delegations'] = deepcopy(old)
    children = ChildResults(saved)

    if durable:
        children.deliver_reports({SESSION: report()})
    else:
        children.record_response(KEY, json.dumps(report()), session_id=SESSION, revision=REVISION)

    assert len(children.resolved) == 16
    assert children.resolved[:-1] == old[1:]
    assert children.resolved[-1]['session_id'] == SESSION
    assert children.resolved[-1]['previous_reason'] == 'workflow_turn_limit'


def test_durable_blocker_replaces_pending_status_and_keeps_its_evidence():
    children = ChildResults(checkpoint({'role': ROLE, 'session_id': SESSION,
                                       'pending': True, 'status': 'partial'}))
    child = report()
    child['resumable'] = True
    child['outcome'] = {'status': 'blocked', 'reason': 'permission_required',
                        'evidence': ['artifact exists'], 'remaining_work': ['User decision'], 'errors': []}

    children.deliver_reports({SESSION: child})

    saved = children.failed[KEY]
    assert saved['status'] == 'blocked'
    assert saved['pending'] is False
    assert saved['resumable'] is True
    assert saved['outcome'] == child['outcome']
    assert children.completed == {}
    assert children.resolved == []

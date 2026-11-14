"""Preserve revision precedence and stable selection when report times tie."""
import pytest

from popot_agents.worker.verification import (
    AMBIGUOUS, acceptance_revision, observed_revision,
)


@pytest.mark.parametrize(('report', 'target', 'expected'), [
    (None, None, 'b'),
    ({}, 'a', 'a'),
    ({'revision': 'a' * 40}, 'b', 'a'),
    ({'revision': ''}, 'a', 'a'),
    ({'revision': 'c' * 40}, 'a', None),
    ({}, 'c', None),
    ({'revision': 'd' * 40}, None, None),
])
def test_observed_revision_preserves_report_target_and_fallback_precedence(report, target, expected):
    receipts = [
        {'revision': 'a' * 40, 'status': 'observed'},
        {'revision': 'b' * 40},
        {'revision': 'c' * 40, 'status': 'failed'},
        {'revision': 'd' * 40, 'status': 'unknown'},
        {'revision': 'invalid', 'status': 'observed'},
    ]
    assert observed_revision(report, receipts, target * 40 if target else None) == (
        expected * 40 if expected else None)


@pytest.mark.parametrize(('reports', 'expected'), [
    ([], None),
    ([('code_reviewer', 'later', 'c')], None),
    ([('backend_engineer', 'later', 'a'), ('frontend_engineer', 'earlier', 'b')], 'a'),
    ([('backend_engineer', 'same', 'a'), ('frontend_engineer', 'same', 'b')], 'b'),
    ([('backend_engineer', '', 'a'), ('frontend_engineer', '', 'b')], 'b'),
    ([('backend_engineer', '2026-11-01T10:00:00+02:00', 'a'),
      ('frontend_engineer', '2026-11-01T09:00:00Z', 'b')], 'a'),
])
def test_acceptance_keeps_last_implementation_at_the_latest_reported_time(reports, expected):
    children = ({'role': role, 'reportedAt': timestamp, 'outcome': {'revision': revision * 40}}
                for role, timestamp, revision in reports)
    assert acceptance_revision(children, [], AMBIGUOUS) == (expected * 40 if expected else None)


@pytest.mark.parametrize(('status', 'source_session', 'expected'), [
    ('completed', 'child', 'c'),
    ('partial', 'child', 'a'),
    ('completed', 'other', 'a'),
])
def test_acceptance_preserves_receipt_order_and_child_attribution(status, source_session, expected):
    receipt = {'revision': 'a' * 40, 'revisionSource': 'pr_head',
               'observedAt': '2026-11-01T10:00:00Z', 'status': 'observed'}
    child_receipt = {**receipt, 'revision': 'c' * 40, 'sourceSessionId': source_session}
    children = [{'sessionId': 'child', 'role': 'code_reviewer',
                 'outcome': {'status': status}, 'observations': [child_receipt]}]
    assert acceptance_revision(children, [receipt, {**receipt, 'revision': 'b' * 40}]) == 'a' * 40
    assert acceptance_revision(children, [{**receipt, 'observedAt': '2026-11-01T09:00:00Z'},
                                          {**receipt, 'revision': 'b' * 40}], None) == 'b' * 40
    assert acceptance_revision(children, [{**receipt, 'observedAt': '2026-11-01T09:00:00Z'}]) == (
        expected * 40)

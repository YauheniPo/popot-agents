"""Intervention SQL paths in the PostgreSQL store, exercised without a live database.

The integration tests in ``test_postgres_store.py`` cover the happy path against a real
server.  These unit tests pin the guard rails around it (unknown session, idempotent
request ids, queue limits and acknowledgement) so a regression cannot silently drop a
comment when no database is available.
"""

import unittest
from contextlib import contextmanager
from datetime import datetime, timezone

from popot_agents.orchestrator.postgres_session_store import PostgresSessionStore

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeCursor:
    def __init__(self, one=None, many=None):
        self._one = one
        self._many = many if many is not None else []

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._many


class FakeConnection:
    """Returns scripted results in call order and records every statement."""

    def __init__(self, results):
        self._results = list(results)
        self.statements = []

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        return self._results.pop(0)


def make_store(results):
    store = PostgresSessionStore.__new__(PostgresSessionStore)
    connection = FakeConnection(results)

    @contextmanager
    def connect():
        yield connection

    store._connect = connect
    return store, connection


class EnqueueInterventionTests(unittest.TestCase):
    def test_invalid_session_id_returns_none_without_querying(self):
        store, connection = make_store([])
        self.assertIsNone(store.enqueue_intervention('not-a-session', 'hello'))
        self.assertEqual(connection.statements, [])

    def test_unknown_session_returns_none(self):
        store, connection = make_store([FakeCursor(one=None)])
        self.assertIsNone(store.enqueue_intervention('a' * 16, 'hello'))
        self.assertEqual(len(connection.statements), 1)

    def test_repeated_request_id_returns_the_original_comment(self):
        prior = ('abc', 'hello', 'queued', 'req-1', NOW)
        store, connection = make_store([FakeCursor(one=('1',)), FakeCursor(one=prior)])
        item = store.enqueue_intervention('a' * 16, 'hello', 'req-1')
        self.assertEqual(item, {'id': 'abc', 'message': 'hello', 'status': 'queued',
                                'requestId': 'req-1', 'createdAt': NOW.isoformat()})
        self.assertEqual(len(connection.statements), 2)

    def test_reused_request_id_with_other_text_is_rejected(self):
        prior = ('abc', 'different', 'queued', 'req-1', NOW)
        store, _ = make_store([FakeCursor(one=('1',)), FakeCursor(one=prior)])
        with self.assertRaisesRegex(ValueError, 'another comment'):
            store.enqueue_intervention('a' * 16, 'hello', 'req-1')

    def test_new_request_id_still_queues_the_comment(self):
        inserted = ('abc', 'hello', 'queued', 'req-2', NOW)
        store, _ = make_store([FakeCursor(one=('1',)), FakeCursor(one=None),
                               FakeCursor(one=(0, 0)), FakeCursor(one=inserted)])
        item = store.enqueue_intervention('a' * 16, 'hello', 'req-2')
        self.assertEqual(item['requestId'], 'req-2')

    def test_queue_is_bounded(self):
        store, _ = make_store([FakeCursor(one=('1',)), FakeCursor(one=(32, 10))])
        with self.assertRaisesRegex(OverflowError, 'too many pending comments'):
            store.enqueue_intervention('a' * 16, 'hello')

    def test_context_budget_is_enforced(self):
        store, _ = make_store([FakeCursor(one=('1',)), FakeCursor(one=(1, 10 ** 9))])
        with self.assertRaisesRegex(OverflowError, 'context budget'):
            store.enqueue_intervention('a' * 16, 'hello')

    def test_insert_returns_the_queued_comment(self):
        inserted = ('abc', 'hello', 'queued', None, NOW)
        store, connection = make_store([FakeCursor(one=('1',)), FakeCursor(one=(0, 0)),
                                        FakeCursor(one=inserted)])
        item = store.enqueue_intervention('a' * 16, 'hello')
        self.assertEqual(item['status'], 'queued')
        self.assertEqual(item['createdAt'], NOW.isoformat())
        self.assertIn('INSERT INTO agent_interventions', connection.statements[-1][0])


class ReadInterventionTests(unittest.TestCase):
    def test_invalid_ids_are_rejected(self):
        store, connection = make_store([])
        self.assertEqual(store.pending_interventions('bad'), [])
        self.assertEqual(store.recent_interventions('bad'), [])
        store.acknowledge_interventions('bad', ['abc'])
        self.assertEqual(connection.statements, [])

    def test_empty_acknowledgement_is_a_noop(self):
        store, connection = make_store([])
        store.acknowledge_interventions('a' * 16, [])
        self.assertEqual(connection.statements, [])

    def test_pending_rows_are_mapped(self):
        store, _ = make_store([FakeCursor(many=[('a', 'one', 'queued', None, NOW),
                                                 ('b', 'two', 'queued', 'r', NOW)])])
        items = store.pending_interventions('a' * 16)
        self.assertEqual([item['id'] for item in items], ['a', 'b'])
        self.assertEqual(items[1]['requestId'], 'r')

    def test_recent_rows_are_returned_oldest_first(self):
        store, _ = make_store([FakeCursor(many=[('new', 'two', 'delivered', None, NOW),
                                                 ('old', 'one', 'queued', None, NOW)])])
        self.assertEqual([item['id'] for item in store.recent_interventions('a' * 16)],
                         ['old', 'new'])

    def test_acknowledgement_marks_delivered_and_prunes_history(self):
        store, connection = make_store([FakeCursor(), FakeCursor()])
        store.acknowledge_interventions('a' * 16, {'abc'})
        self.assertEqual(len(connection.statements), 2)
        self.assertIn('status=\'delivered\'', connection.statements[0][0])
        self.assertEqual(connection.statements[0][1][1], ['abc'])
        self.assertIn('OFFSET 100', connection.statements[1][0])


if __name__ == '__main__':
    unittest.main()

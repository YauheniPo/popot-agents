"""Comments arriving during a turn survive saves and remain queued until acknowledged."""

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from popot_agents.orchestrator.session_store import SessionStore


class InterventionStoreTests(unittest.TestCase):
    def test_comment_is_durable_idempotent_and_acknowledged(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(directory)
            sid = 'a' * 16
            store.save(sid, 'agent', [], 'planner', {})
            first = store.enqueue_intervention(sid, 'Use the new API', 'comment-1')
            self.assertEqual(store.enqueue_intervention(sid, 'Use the new API', 'comment-1')['id'], first['id'])
            with self.assertRaisesRegex(ValueError, 'another comment'):
                store.enqueue_intervention(sid, 'Different', 'comment-1')
            store.save(sid, 'agent', [{'role': 'user', 'content': 'original'}], 'planner', {})
            reopened = SessionStore(directory)
            self.assertEqual(reopened.pending_interventions(sid)[0]['id'], first['id'])
            reopened.acknowledge_interventions(sid, [first['id']])
            self.assertEqual(reopened.pending_interventions(sid), [])
            self.assertEqual(reopened.get(sid)['interventions'][0]['status'], 'delivered')

    def test_pending_queue_is_bounded_without_dropping_older_comments(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(directory)
            sid = 'b' * 16
            store.save(sid, 'agent', [], 'planner', {})
            for index in range(32):
                store.enqueue_intervention(sid, f'comment {index}')
            with self.assertRaisesRegex(OverflowError, 'pending'):
                store.enqueue_intervention(sid, 'overflow')
            self.assertEqual([item['message'] for item in store.pending_interventions(sid)],
                             [f'comment {index}' for index in range(32)])

    def test_parallel_comments_are_not_lost(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(directory)
            sid = 'c' * 16
            store.save(sid, 'agent', [], 'planner', {})
            with ThreadPoolExecutor(max_workers=8) as pool:
                items = list(pool.map(lambda index: store.enqueue_intervention(
                    sid, f'comment {index}', f'call-{index}'), range(8)))
            self.assertEqual(len({item['id'] for item in items}), 8)
            self.assertEqual({item['message'] for item in store.pending_interventions(sid)},
                             {f'comment {index}' for index in range(8)})

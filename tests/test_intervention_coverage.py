"""Coverage for intervention plumbing: store edge cases, worker poller and A2A handlers."""

import asyncio
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from popot_agents.orchestrator.session_store import SessionStore
from popot_agents.worker import http_harness


class StoreInterventionEdgeTests(unittest.TestCase):
    def test_unknown_session_returns_none(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(SessionStore(directory).enqueue_intervention('d' * 16, 'hello'))

    def test_pending_comments_respect_context_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(directory)
            sid = 'e' * 16
            store.save(sid, 'agent', [], 'planner', {})
            with self.assertRaisesRegex(OverflowError, 'context budget'):
                store.enqueue_intervention(sid, 'x' * 500001)

    def test_oldest_delivered_comment_is_pruned_at_the_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(directory)
            sid = 'f' * 16
            store.save(sid, 'agent', [], 'planner', {})
            items = [{'id': f'{index:032x}', 'message': 'old', 'status': 'delivered',
                      'requestId': None, 'createdAt': '2026-01-01T00:00:00+00:00'}
                     for index in range(100)]
            store._save(sid, 'agent', [], 'planner', {}, interventions=items)
            item = store.enqueue_intervention(sid, 'fresh')
            saved = store.get(sid)['interventions']
            self.assertEqual(saved[-1]['id'], item['id'])
            self.assertEqual(len(saved), 100)
            self.assertNotIn(items[0]['id'], {entry['id'] for entry in saved})

    def test_cap_without_delivered_entries_keeps_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(directory)
            sid = '1' * 16
            store.save(sid, 'agent', [], 'planner', {})
            items = [{'id': f'{index:032x}', 'message': 'old', 'status': 'expired',
                      'requestId': None, 'createdAt': '2026-01-01T00:00:00+00:00'}
                     for index in range(100)]
            store._save(sid, 'agent', [], 'planner', {}, interventions=items)
            store.enqueue_intervention(sid, 'fresh')
            self.assertEqual(len(store.get(sid)['interventions']), 101)


class PendingInterventionPollTests(unittest.TestCase):
    def test_missing_credentials_skip_polling(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(http_harness._pending_interventions(), [])

    def test_valid_entries_are_filtered(self):
        payload = {'interventions': [{'id': 'a', 'message': 'm'}, {'id': 2}, 'junk',
                                     {'id': 'b', 'message': 3}]}
        with mock.patch.dict(os.environ, {'AX_DELEGATION_URL': 'http://ax/',
                                          'AX_DELEGATION_TOKEN': 't'}), \
                mock.patch.object(http_harness.request, 'urlopen',
                                  return_value=io.BytesIO(json.dumps(payload).encode())):
            self.assertEqual(http_harness._pending_interventions(), [{'id': 'a', 'message': 'm'}])

    def test_non_list_payload_is_rejected(self):
        payload = {'interventions': 'nope'}
        with mock.patch.dict(os.environ, {'AX_DELEGATION_URL': 'http://ax',
                                          'AX_DELEGATION_TOKEN': 't'}), \
                mock.patch.object(http_harness.request, 'urlopen',
                                  return_value=io.BytesIO(json.dumps(payload).encode())):
            self.assertEqual(http_harness._pending_interventions(), [])

    def test_transport_failure_is_tolerated(self):
        with mock.patch.dict(os.environ, {'AX_DELEGATION_URL': 'http://ax',
                                          'AX_DELEGATION_TOKEN': 't'}), \
                mock.patch.object(http_harness.request, 'urlopen', side_effect=OSError('down')):
            self.assertEqual(http_harness._pending_interventions(), [])


class InterventionsHandlerTests(unittest.TestCase):
    def _call(self, service):
        from ax_local.api import a2a_sdk
        call = mock.Mock(headers={'authorization': 'Bearer token'})
        return asyncio.run(a2a_sdk._interventions_handler(service, call))

    def test_returns_pending_items(self):
        service = mock.Mock()
        service.pending_interventions.return_value = [{'id': 'a'}]
        response = self._call(service)
        self.assertEqual(response.status_code, 200)
        service.pending_interventions.assert_called_once_with('token')

    def test_expired_capability_maps_to_forbidden(self):
        from ax_local.api import a2a_sdk
        service = mock.Mock()
        service.pending_interventions.side_effect = a2a_sdk.A2AError.__new__(a2a_sdk.A2AError)
        self.assertEqual(self._call(service).status_code, 403)

    def test_unreachable_service_maps_to_bad_gateway(self):
        service = mock.Mock()
        service.pending_interventions.side_effect = OSError('down')
        self.assertEqual(self._call(service).status_code, 502)

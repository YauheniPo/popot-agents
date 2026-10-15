import os
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


@unittest.skipUnless(os.getenv("AGENT_TEST_POSTGRES"), "requires a PostgreSQL test database")
class PostgresSessionStoreTests(unittest.TestCase):
    def setUp(self):
        from popot_agents.orchestrator.postgres_session_store import PostgresSessionStore

        self.store = PostgresSessionStore()
        self.session_id = uuid.uuid4().hex[:16]

    def tearDown(self):
        self.store.delete(self.session_id)

    def test_history_role_and_creation_time_survive_reconnection(self):
        from popot_agents.orchestrator.postgres_session_store import PostgresSessionStore

        role = {"agent": "test_agent", "instructions": "Original instructions", "tools": []}
        self.store.save(self.session_id, "test_agent", [], "analyst", role)
        created_at = self.store.get(self.session_id)["createdAt"]
        history = [{"role": "user", "content": "hello"},
                   {"role": "assistant", "content": "hi"}]
        self.store.save(self.session_id, "test_agent", history, "analyst", role)
        restored = PostgresSessionStore().get(self.session_id)
        self.assertEqual(restored["messages"], history)
        self.assertEqual(restored["role"], "analyst")
        self.assertEqual(restored["roleConfig"], role)
        self.assertEqual(restored["createdAt"], created_at)

    def test_recent_update_does_not_extend_seven_day_retention(self):
        import psycopg

        self.store.save(self.session_id, "test_agent", [], "analyst", {})
        with psycopg.connect(connect_timeout=5) as connection:
            connection.execute(
                "UPDATE agent_sessions SET created_at = %s, updated_at = %s WHERE session_id = %s",
                (datetime.now(timezone.utc) - timedelta(days=8),
                 datetime.now(timezone.utc), self.session_id),
            )
        self.assertIsNone(self.store.get(self.session_id))
        self.assertIn(self.session_id, self.store.prune_expired())
        self.assertIsNone(self.store.get(self.session_id))

    def test_zero_ttl_keeps_session_past_seven_days(self):
        import psycopg

        role = {"agent": "test_agent", "instructions": "x", "tools": [], "ttl_seconds": 0}
        self.store.save(self.session_id, "test_agent", [], "analyst", role)
        with psycopg.connect(connect_timeout=5) as connection:
            connection.execute(
                "UPDATE agent_sessions SET created_at = %s WHERE session_id = %s",
                (datetime.now(timezone.utc) - timedelta(days=8), self.session_id),
            )
        self.assertIsNotNone(self.store.get(self.session_id))
        self.assertIsNone(self.store.expires_at(self.store.get(self.session_id)))
        self.assertNotIn(self.session_id, self.store.prune_expired())

    def test_ax_history_survives_retention_and_all_pruning_paths(self):
        import psycopg

        role = {'_retain_history': True, 'ttl_seconds': 900}
        history = [{'role': 'user', 'content': 'original task'},
                   {'role': 'assistant', 'content': 'previous answer'}]
        self.store.save(self.session_id, 'test_agent', history, None, role)
        with psycopg.connect(connect_timeout=5) as connection:
            connection.execute(
                'UPDATE agent_sessions SET created_at = %s WHERE session_id = %s',
                (datetime.now(timezone.utc) - timedelta(days=30), self.session_id),
            )
        saved = self.store.get(self.session_id)
        self.assertEqual(saved['messages'], history)
        self.assertIsNone(self.store.expires_at(saved))
        self.assertIn(self.session_id, {chat['sessionId'] for chat in self.store.list_chats()})
        self.assertNotIn(self.session_id, self.store.expired_ids())
        self.assertFalse(self.store.delete_expired(self.session_id))
        self.assertNotIn(self.session_id, self.store.prune_expired())

    def test_imports_legacy_json_session_without_replacing_existing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            created_at = datetime.now(timezone.utc).isoformat()
            old = {
                "sessionId": self.session_id, "agent": "test_agent", "role": "analyst",
                "roleConfig": {"instructions": "Original"},
                "messages": [{"role": "user", "content": "old chat"},
                             {"role": "assistant", "content": "answer"}],
                "updatedAt": created_at,
            }
            (Path(directory) / f"{self.session_id}.json").write_text(json.dumps(old))
            self.store.import_legacy(directory)
            saved = self.store.get(self.session_id)
            self.assertEqual(saved["messages"], old["messages"])
            self.assertEqual(saved["roleConfig"], old["roleConfig"])
            self.assertEqual(saved["createdAt"], created_at)
            self.store.delete(self.session_id)
            self.store.import_legacy(directory)
            self.assertIsNone(self.store.get(self.session_id))


if __name__ == "__main__":
    unittest.main()

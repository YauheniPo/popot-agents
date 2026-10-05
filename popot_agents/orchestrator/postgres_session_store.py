"""PostgreSQL storage for Docker orchestrator chat sessions."""

import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from popot_agents.runtime_config import RUNTIME
from .session_store import SESSION_RETENTION, SessionStore


class PostgresSessionStore:
    def __init__(self) -> None:
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS agent_sessions (
                    session_id text PRIMARY KEY CHECK (session_id ~ '^[0-9a-f]{16}$'),
                    agent text NOT NULL,
                    role text,
                    role_config jsonb,
                    messages jsonb NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now()
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS agent_session_legacy_imports (
                    session_id text PRIMARY KEY
                )
            """)

    @staticmethod
    @contextmanager
    def _connect():
        try:
            with psycopg.connect(connect_timeout=RUNTIME["timeouts"]["postgres_connect_seconds"]) as connection:
                yield connection
        except psycopg.Error as exc:
            raise OSError("session database unavailable") from exc

    @staticmethod
    def _valid_id(session_id: str) -> bool:
        return isinstance(session_id, str) and re.fullmatch(r"[0-9a-f]{16}", session_id) is not None

    @staticmethod
    def _saved(row) -> dict:
        return {
            "sessionId": row[0], "agent": row[1], "role": row[2],
            "roleConfig": row[3], "messages": row[4],
            "createdAt": row[5].isoformat(), "updatedAt": row[6].isoformat(),
        }

    expires_at = staticmethod(SessionStore.expires_at)

    def get(self, session_id: str) -> dict | None:
        if not self._valid_id(session_id):
            return None
        with self._connect() as connection:
            row = connection.execute("""
                SELECT session_id, agent, role, role_config, messages, created_at, updated_at
                FROM agent_sessions
                WHERE session_id = %s AND (created_at > now() - %s::interval
                    OR role_config->>'ttl_seconds' = '0')
            """, (session_id, SESSION_RETENTION)).fetchone()
        return self._saved(row) if row else None

    def list_chats(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute("""
                SELECT session_id, agent, role, role_config, messages, created_at, updated_at
                FROM agent_sessions
                WHERE created_at > now() - %s::interval
                    OR role_config->>'ttl_seconds' = '0'
                ORDER BY updated_at DESC
            """, (SESSION_RETENTION,)).fetchall()
        return [self._saved(row) for row in rows]

    def save(self, session_id: str, agent: str, messages: list[dict[str, str]],
             role: str | None = None, role_config: dict | None = None) -> None:
        if not self._valid_id(session_id):
            raise ValueError("invalid session ID")
        with self._connect() as connection:
            connection.execute("""
                INSERT INTO agent_sessions (session_id, agent, role, role_config, messages)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (session_id) DO UPDATE SET
                    agent = EXCLUDED.agent,
                    role = EXCLUDED.role,
                    role_config = EXCLUDED.role_config,
                    messages = EXCLUDED.messages,
                    updated_at = now()
            """, (session_id, agent, role,
                  Jsonb(role_config) if role_config is not None else None,
                  Jsonb(messages)))

    def prune_expired(self) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute("""
                DELETE FROM agent_sessions
                WHERE created_at <= now() - %s::interval
                    AND (role_config->>'ttl_seconds' IS DISTINCT FROM '0')
                RETURNING session_id
            """, (SESSION_RETENTION,)).fetchall()
        return {row[0] for row in rows}

    def expired_ids(self) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute("""
                SELECT session_id FROM agent_sessions
                WHERE created_at <= now() - %s::interval
                    AND (role_config->>'ttl_seconds' IS DISTINCT FROM '0')
            """, (SESSION_RETENTION,)).fetchall()
        return {row[0] for row in rows}

    def delete_expired(self, session_id: str) -> bool:
        if not self._valid_id(session_id):
            return False
        with self._connect() as connection:
            rows = connection.execute("""
                DELETE FROM agent_sessions
                WHERE session_id = %s AND created_at <= now() - %s::interval
                    AND (role_config->>'ttl_seconds' IS DISTINCT FROM '0')
                RETURNING session_id
            """, (session_id, SESSION_RETENTION)).fetchall()
        return bool(rows)

    def delete(self, session_id: str) -> None:
        if not self._valid_id(session_id):
            return
        with self._connect() as connection:
            connection.execute("DELETE FROM agent_sessions WHERE session_id = %s",
                               (session_id,))

    def import_legacy(self, directory: str | Path) -> None:
        source = Path(directory)
        if not source.is_dir():
            return
        with self._connect() as connection:
            for path in source.glob("[0-9a-f]" * 16 + ".json"):
                try:
                    saved = json.loads(path.read_text(encoding="utf-8"))
                    if saved["sessionId"] != path.stem or not self._valid_id(path.stem):
                        continue
                    created_at = datetime.fromisoformat(
                        saved.get("createdAt", saved["updatedAt"]))
                    updated_at = datetime.fromisoformat(saved["updatedAt"])
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                    if updated_at.tzinfo is None:
                        updated_at = updated_at.replace(tzinfo=timezone.utc)
                    if created_at + SESSION_RETENTION <= datetime.now(timezone.utc) \
                            and (saved.get("roleConfig") or {}).get("ttl_seconds") != 0:
                        continue
                    agent, messages = saved["agent"], saved["messages"]
                    if not isinstance(agent, str) or not isinstance(messages, list):
                        continue
                except (OSError, ValueError, KeyError, TypeError):
                    continue
                claimed = connection.execute("""
                    INSERT INTO agent_session_legacy_imports (session_id)
                    VALUES (%s) ON CONFLICT (session_id) DO NOTHING
                    RETURNING session_id
                """, (path.stem,)).fetchone()
                if claimed is None:
                    continue
                connection.execute("""
                    INSERT INTO agent_sessions
                        (session_id, agent, role, role_config, messages, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (session_id) DO NOTHING
                """, (path.stem, agent, saved.get("role"),
                      Jsonb(saved.get("roleConfig")) if saved.get("roleConfig") is not None else None,
                      Jsonb(messages), created_at, updated_at))

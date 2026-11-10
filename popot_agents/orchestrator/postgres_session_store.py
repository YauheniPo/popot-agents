"""PostgreSQL storage for AX API chat sessions."""

import json
import re
import secrets
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
            connection.execute("""
                CREATE TABLE IF NOT EXISTS agent_interventions (
                    sequence bigserial UNIQUE,
                    id text PRIMARY KEY CHECK (id ~ '^[0-9a-f]{32}$'),
                    session_id text NOT NULL REFERENCES agent_sessions(session_id) ON DELETE CASCADE,
                    request_id text,
                    message text NOT NULL,
                    status text NOT NULL CHECK (status IN ('queued', 'delivered')),
                    created_at timestamptz NOT NULL DEFAULT now(),
                    UNIQUE (session_id, request_id)
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
                    OR role_config->>'ttl_seconds' = '0'
                    OR role_config->>'_retain_history' = 'true')
            """, (session_id, SESSION_RETENTION)).fetchone()
        return self._saved(row) if row else None

    def list_chats(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute("""
                SELECT session_id, agent, role, role_config, messages, created_at, updated_at
                FROM agent_sessions
                WHERE created_at > now() - %s::interval
                    OR role_config->>'ttl_seconds' = '0'
                    OR role_config->>'_retain_history' = 'true'
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
                    role_config = CASE WHEN agent_sessions.role_config ? '_work_evidence'
                        THEN COALESCE(EXCLUDED.role_config, '{}'::jsonb) || jsonb_build_object(
                            '_work_evidence', agent_sessions.role_config->'_work_evidence')
                        ELSE EXCLUDED.role_config END,
                    messages = EXCLUDED.messages,
                    updated_at = now()
            """, (session_id, agent, role,
                  Jsonb(role_config) if role_config is not None else None,
                  Jsonb(messages)))

    def record_evidence(self, session_id, receipt):
        from popot_agents.work_evidence import retain_evidence
        if not self._valid_id(session_id):
            raise ValueError('invalid session ID')
        with self._connect() as connection:
            row = connection.execute('SELECT role_config FROM agent_sessions WHERE session_id=%s FOR UPDATE',
                                     (session_id,)).fetchone()
            if row is None:
                return
            evidence = retain_evidence((row[0] or {}).get('_work_evidence', []), receipt)
            connection.execute("""UPDATE agent_sessions SET role_config = jsonb_set(
                COALESCE(role_config, '{}'::jsonb), '{_work_evidence}', %s), updated_at=now()
                WHERE session_id=%s""", (Jsonb(evidence), session_id))

    @staticmethod
    def _intervention(row):
        return {'id': row[0], 'message': row[1], 'status': row[2],
                'requestId': row[3], 'createdAt': row[4].isoformat()}

    def enqueue_intervention(self, session_id, message, request_id=None):
        if not self._valid_id(session_id):
            return None
        with self._connect() as connection:
            if connection.execute('SELECT 1 FROM agent_sessions WHERE session_id=%s FOR UPDATE',
                                  (session_id,)).fetchone() is None:
                return None
            if request_id is not None:
                prior = connection.execute('''SELECT id,message,status,request_id,created_at
                    FROM agent_interventions WHERE session_id=%s AND request_id=%s''',
                    (session_id, request_id)).fetchone()
                if prior is not None:
                    if prior[1] != message:
                        raise ValueError('requestId belongs to another comment')
                    return self._intervention(prior)
            count, size = connection.execute('''SELECT count(*),COALESCE(sum(octet_length(message)),0)
                FROM agent_interventions WHERE session_id=%s AND status='queued' ''',
                (session_id,)).fetchone()
            if count >= 32:
                raise OverflowError('too many pending comments')
            if size + len(message.encode('utf-8')) > RUNTIME['limits']['history_bytes'] // 4:
                raise OverflowError('pending comments exceed the context budget')
            row = connection.execute('''INSERT INTO agent_interventions
                (id,session_id,request_id,message,status) VALUES (%s,%s,%s,%s,'queued')
                RETURNING id,message,status,request_id,created_at''',
                (secrets.token_hex(16), session_id, request_id, message)).fetchone()
            return self._intervention(row)

    def pending_interventions(self, session_id):
        if not self._valid_id(session_id):
            return []
        with self._connect() as connection:
            rows = connection.execute('''SELECT id,message,status,request_id,created_at
                FROM agent_interventions WHERE session_id=%s AND status='queued'
                ORDER BY sequence LIMIT 32''', (session_id,)).fetchall()
        return [self._intervention(row) for row in rows]

    def recent_interventions(self, session_id):
        if not self._valid_id(session_id):
            return []
        with self._connect() as connection:
            rows = connection.execute('''SELECT id,message,status,request_id,created_at
                FROM agent_interventions WHERE session_id=%s
                AND (status='queued' OR id IN (
                    SELECT id FROM agent_interventions WHERE session_id=%s AND status='delivered'
                    ORDER BY sequence DESC LIMIT 100))
                ORDER BY sequence DESC''', (session_id, session_id)).fetchall()
        return [self._intervention(row) for row in reversed(rows)]

    def acknowledge_interventions(self, session_id, ids):
        if not self._valid_id(session_id) or not ids:
            return
        with self._connect() as connection:
            connection.execute('''UPDATE agent_interventions SET status='delivered'
                WHERE session_id=%s AND id=ANY(%s) AND status='queued' ''', (session_id, list(ids)))
            connection.execute('''DELETE FROM agent_interventions WHERE session_id=%s AND id IN (
                SELECT id FROM agent_interventions WHERE session_id=%s AND status='delivered'
                ORDER BY sequence DESC OFFSET 100)''', (session_id, session_id))

    def prune_expired(self) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute("""
                DELETE FROM agent_sessions
                WHERE created_at <= now() - %s::interval
                    AND (role_config->>'ttl_seconds' IS DISTINCT FROM '0')
                    AND (role_config->>'_retain_history' IS DISTINCT FROM 'true')
                RETURNING session_id
            """, (SESSION_RETENTION,)).fetchall()
        return {row[0] for row in rows}

    def expired_ids(self) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute("""
                SELECT session_id FROM agent_sessions
                WHERE created_at <= now() - %s::interval
                    AND (role_config->>'ttl_seconds' IS DISTINCT FROM '0')
                    AND (role_config->>'_retain_history' IS DISTINCT FROM 'true')
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
                    AND (role_config->>'_retain_history' IS DISTINCT FROM 'true')
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
                            and (saved.get("roleConfig") or {}).get("ttl_seconds") != 0 \
                            and (saved.get("roleConfig") or {}).get("_retain_history") is not True:
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

"""Small durable store for chat transcripts on the API host."""

import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from popot_agents.runtime_config import RUNTIME

SESSION_RETENTION = timedelta(days=RUNTIME["sessions"]["retention_days"])


class SessionStore:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def _path(self, session_id: str) -> Path | None:
        if not re.fullmatch(r"[0-9a-f]{16}", session_id):
            return None
        return self.directory / f"{session_id}.json"

    def get(self, session_id: str) -> dict | None:
        path = self._path(session_id)
        if path is None or not path.exists():
            return None
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise OSError("invalid session file") from exc
        return None if self._expired(saved) else saved

    @staticmethod
    def expires_at(saved: dict) -> datetime | None:
        if (saved.get("roleConfig") or {}).get("ttl_seconds") == 0:
            return None
        # Older session files have only updatedAt; use it when creation is unknown.
        created = datetime.fromisoformat(saved.get("createdAt", saved["updatedAt"]))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return created + SESSION_RETENTION

    @classmethod
    def _expired(cls, saved: dict) -> bool:
        expiry = cls.expires_at(saved)
        return expiry is not None and datetime.now(timezone.utc) >= expiry

    def prune_expired(self) -> set[str]:
        if not self.directory.exists():
            return set()
        expired = set()
        for path in self.directory.glob("[0-9a-f]" * 16 + ".json"):
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if self._expired(saved):
                path.unlink(missing_ok=True)
                expired.add(path.stem)
        return expired

    def list_chats(self) -> list[dict]:
        if not self.directory.exists():
            return []
        chats = [saved for path in self.directory.glob("[0-9a-f]" * 16 + ".json")
                 if (saved := self.get(path.stem)) is not None]
        return sorted(chats, key=lambda chat: chat["updatedAt"], reverse=True)

    def save(self, session_id: str, agent: str, messages: list[dict[str, str]],
             role: str | None = None, role_config: dict | None = None) -> None:
        path = self._path(session_id)
        if path is None:
            raise ValueError("invalid session ID")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        now = datetime.now(timezone.utc).isoformat()
        try:
            previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        except json.JSONDecodeError as exc:
            raise OSError("invalid session file") from exc
        created_at = (previous.get("createdAt", previous["updatedAt"])
                      if previous else now)
        payload = {
            "sessionId": session_id,
            "agent": agent,
            "role": role,
            "roleConfig": role_config,
            "messages": messages,
            "createdAt": created_at,
            "updatedAt": now,
        }
        descriptor, temporary = tempfile.mkstemp(prefix=f".{session_id}-", dir=self.directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def delete(self, session_id: str) -> None:
        path = self._path(session_id)
        if path is not None:
            path.unlink(missing_ok=True)

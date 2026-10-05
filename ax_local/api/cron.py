"""Run configured AX roles on five-field UTC cron schedules."""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from datetime import datetime, timedelta, timezone

from popot_agents.orchestrator.cron import cron_matches, validate_cron


class CronScheduler:
    def __init__(self, roles: dict, runner, store, *, max_concurrent_tasks: int):
        self.entries = [(role, index, entry) for role, config in roles.items()
                        for index, entry in enumerate(config.get("cron", []))]
        self.roles, self.runner, self.store = roles, runner, store
        self.slots = threading.BoundedSemaphore(max_concurrent_tasks)
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.active: set[tuple[str, int]] = set()
        self.active_sessions: set[str] = set()
        self.jobs: set[threading.Thread] = set()
        self.chats: dict[tuple[str, int], object] = {}
        self.thread: threading.Thread | None = None

    def is_active(self, session_id: str) -> bool:
        with self.lock:
            return session_id in self.active_sessions

    def recover_interrupted(self) -> None:
        for saved in self.store.list_chats():
            config = saved.get("roleConfig") or {}
            meta = config.get("_ax_cron")
            if meta and meta.get("state") in {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}:
                self.runner.close_session(saved["sessionId"])
                meta.update(state="TASK_STATE_FAILED", error="AX API restarted during cron run")
                self.store.save(saved["sessionId"], saved["agent"], saved["messages"],
                                saved["role"], config)

    def start(self) -> None:
        if self.entries and self.thread is None:
            self.thread = threading.Thread(target=self._loop, name="ax-cron", daemon=True)
            self.thread.start()

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            now = datetime.now(timezone.utc)
            try:
                self.run_due(now)
            except Exception as exc:
                print(json.dumps({"event": "ax_cron", "state": "ERROR",
                                  "error_type": type(exc).__name__}), file=sys.stderr, flush=True)
            next_minute = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
            self.stop_event.wait(max(0, (next_minute - datetime.now(timezone.utc)).total_seconds()))

    def run_due(self, now: datetime) -> None:
        minute = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
        with self.lock:
            if self.stop_event.is_set():
                return
            for role, index, entry in self.entries:
                key = (role, index)
                if key in self.active or not cron_matches(entry["schedule"], minute):
                    continue
                identity = f'{role}\0{index}\0{entry["schedule"]}\0{entry["task"]}\0{minute.isoformat()}'
                session_id = hashlib.sha256(identity.encode()).hexdigest()[:16]
                if self.store.get(session_id) is not None:
                    continue
                if not self.slots.acquire(blocking=False):
                    print(json.dumps({"event": "ax_cron", "role": role, "schedule": index,
                                      "state": "SKIPPED", "reason": "capacity full"}),
                          file=sys.stderr, flush=True)
                    continue
                config = {key: value for key, value in self.roles[role].items() if key != "cron"}
                config["_ax_cron"] = {"schedule_index": index, "expression": entry["schedule"],
                                      "scheduled_at": minute.isoformat(),
                                      "state": "TASK_STATE_SUBMITTED"}
                try:
                    self.store.save(session_id, config["agent"], [], role, config)
                    self.active.add(key)
                    self.active_sessions.add(session_id)
                    thread = threading.Thread(target=self._execute,
                                              args=(key, session_id, config, entry["task"]), daemon=True)
                    self.jobs.add(thread)
                    thread.start()
                except Exception:
                    self.active.discard(key)
                    self.active_sessions.discard(session_id)
                    self.slots.release()
                    raise

    def _execute(self, key: tuple[str, int], session_id: str, config: dict, task: str) -> None:
        chat = None
        messages = []
        stage = "worker_startup"
        try:
            config["_ax_cron"]["state"] = "TASK_STATE_WORKING"
            self.store.save(session_id, config["agent"], [], key[0], config)
            chat = self.runner.start_chat(session_id, config, cancel_event=self.stop_event)
            with self.lock:
                self.chats[key] = chat
            if self.stop_event.is_set():
                raise RuntimeError("scheduler stopped")
            stage = "worker_message"
            answer = chat.send(task)["answer"]
            messages = [{"role": "user", "content": task},
                        {"role": "assistant", "content": answer}]
            config["_ax_cron"]["state"] = "TASK_STATE_COMPLETED"
        except Exception as exc:
            config["_ax_cron"].update(state="TASK_STATE_FAILED", stage=stage,
                                       error=type(exc).__name__)
        finally:
            if chat is not None:
                try:
                    chat.close()
                except Exception as exc:
                    print(json.dumps({"event": "ax_cron", "session_id": session_id,
                                      "state": "CLOSE_ERROR", "error_type": type(exc).__name__}),
                          file=sys.stderr, flush=True)
            try:
                self.store.save(session_id, config["agent"], messages, key[0], config)
                print(json.dumps({"event": "ax_cron", "session_id": session_id,
                                  "role": key[0], "state": config["_ax_cron"]["state"]}),
                      file=sys.stderr, flush=True)
            finally:
                with self.lock:
                    self.chats.pop(key, None)
                    self.active.discard(key)
                    self.active_sessions.discard(session_id)
                    self.jobs.discard(threading.current_thread())
                self.slots.release()

    def wait_for_jobs(self) -> None:
        while True:
            with self.lock:
                jobs = tuple(self.jobs)
            if not jobs:
                return
            for job in jobs:
                job.join()

    def close(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join()
        with self.lock:
            chats = tuple(self.chats.values())
        for chat in chats:
            chat.close()

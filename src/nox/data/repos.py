"""Typed repositories over `Database` (ADR-006, Data Model L2). Rows are pydantic models; no ORM.

Time handling: every timestamp is stored as ISO-8601 UTC text; callers pass aware datetimes (naive
values are treated as UTC). Retention: `TurnRepository.purge_expired`,
`TemporaryGrantRepository.purge`,
`HealthHistoryRepository.purge_older_than` and `StateCheckpointRepository.prune` implement the
cleanup
rules of the Data Model and the Failure and Recovery Model.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from nox.data.db import Database


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _now() -> datetime:
    return datetime.now(UTC)


class Row(BaseModel):
    model_config = ConfigDict(frozen=True)


# ---- state checkpoints ---------------------------------------------------------------------------


class StateCheckpointRow(Row):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    id: int
    version: int
    ts: datetime
    reason: str
    state_json: str = Field(alias="json")  # column `json`


class StateCheckpointRepository:
    KEEP = 200

    def __init__(self, db: Database, *, keep: int = KEEP) -> None:
        self._db = db
        self._keep = keep

    def save(self, version: int, reason: str, state_json: str) -> int:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO state_checkpoints (version, ts, reason, json) VALUES (?, ?, ?, ?)",
                (version, _iso(_now()), reason, state_json),
            )
            self._prune(conn)
        return int(cur.lastrowid or 0)

    def latest(self) -> StateCheckpointRow | None:
        row = self._db.fetch_one("SELECT * FROM state_checkpoints ORDER BY id DESC LIMIT 1")
        return None if row is None else StateCheckpointRow(**dict(row))

    def prune(self, keep: int | None = None) -> int:
        with self._db.transaction() as conn:
            return self._prune(conn, keep)

    def _prune(self, conn: sqlite3.Connection, keep: int | None = None) -> int:
        limit = self._keep if keep is None else keep
        cur = conn.execute(
            "DELETE FROM state_checkpoints WHERE id NOT IN "
            "(SELECT id FROM state_checkpoints ORDER BY id DESC LIMIT ?)",
            (limit,),
        )
        return int(cur.rowcount)

    def count(self) -> int:
        row = self._db.fetch_one("SELECT COUNT(*) FROM state_checkpoints")
        return int(row[0]) if row else 0


# ---- health history ------------------------------------------------------------------------------


class HealthHistoryRow(Row):
    id: int
    ts: datetime
    component: str
    status: str
    reason: str


class HealthHistoryRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(self, component: str, status: str, reason: str = "", ts: datetime | None = None) -> int:
        cur = self._db.execute(
            "INSERT INTO health_history (ts, component, status, reason) VALUES (?, ?, ?, ?)",
            (_iso(ts or _now()), component, status, reason),
        )
        return int(cur.lastrowid or 0)

    def list_recent(self, limit: int = 100, component: str | None = None) -> list[HealthHistoryRow]:
        if component is None:
            rows = self._db.fetch_all(
                "SELECT * FROM health_history ORDER BY id DESC LIMIT ?", (limit,)
            )
        else:
            rows = self._db.fetch_all(
                "SELECT * FROM health_history WHERE component = ? ORDER BY id DESC LIMIT ?",
                (component, limit),
            )
        return [HealthHistoryRow(**dict(r)) for r in rows]

    def latest_per_component(self) -> dict[str, HealthHistoryRow]:
        rows = self._db.fetch_all(
            "SELECT h.* FROM health_history h JOIN "
            "(SELECT component, MAX(id) AS id FROM health_history GROUP BY component) m "
            "ON h.id = m.id ORDER BY h.component"
        )
        return {str(r["component"]): HealthHistoryRow(**dict(r)) for r in rows}

    def purge_older_than(self, days: int = 30, now: datetime | None = None) -> int:
        cutoff = _iso((now or _now()) - timedelta(days=days))
        cur = self._db.execute("DELETE FROM health_history WHERE ts < ?", (cutoff,))
        return int(cur.rowcount)


# ---- sessions & turns ----------------------------------------------------------------------------


class SessionRow(Row):
    id: str
    started_at: datetime
    ended_at: datetime | None = None
    mode: str
    privacy_mode: str
    summary: str = ""


class TurnRow(Row):
    id: int
    session_id: str
    ts: datetime
    role: str
    text: str
    provider: str = ""
    latency_ms: int | None = None
    retain_until: datetime | None = None


class SessionRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def create(
        self,
        mode: str,
        privacy_mode: str,
        *,
        session_id: str | None = None,
        started_at: datetime | None = None,
    ) -> SessionRow:
        sid = session_id or str(uuid.uuid4())
        started = started_at or _now()
        self._db.execute(
            "INSERT INTO sessions (id, started_at, mode, privacy_mode) VALUES (?, ?, ?, ?)",
            (sid, _iso(started), mode, privacy_mode),
        )
        row = self.get(sid)
        assert row is not None
        return row

    def get(self, session_id: str) -> SessionRow | None:
        row = self._db.fetch_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return None if row is None else SessionRow(**dict(row))

    def end(self, session_id: str, summary: str = "", ended_at: datetime | None = None) -> bool:
        cur = self._db.execute(
            "UPDATE sessions SET ended_at = ?, summary = ? WHERE id = ? AND ended_at IS NULL",
            (_iso(ended_at or _now()), summary, session_id),
        )
        return cur.rowcount == 1

    def list_recent(self, limit: int = 20) -> list[SessionRow]:
        rows = self._db.fetch_all(
            "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,)
        )
        return [SessionRow(**dict(r)) for r in rows]

    def active(self) -> SessionRow | None:
        row = self._db.fetch_one(
            "SELECT * FROM sessions WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
        )
        return None if row is None else SessionRow(**dict(row))


class TurnRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(
        self,
        session_id: str,
        role: str,
        text: str,
        *,
        provider: str = "",
        latency_ms: int | None = None,
        retain_until: datetime | None = None,
        ts: datetime | None = None,
    ) -> int:
        cur = self._db.execute(
            "INSERT INTO turns (session_id, ts, role, text, provider, latency_ms, retain_until) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, _iso(ts or _now()), role, text, provider, latency_ms, _iso(retain_until)),
        )
        return int(cur.lastrowid or 0)

    def get(self, turn_id: int) -> TurnRow | None:
        row = self._db.fetch_one("SELECT * FROM turns WHERE id = ?", (turn_id,))
        return None if row is None else TurnRow(**dict(row))

    def list_for_session(self, session_id: str, limit: int = 1000) -> list[TurnRow]:
        rows = self._db.fetch_all(
            "SELECT * FROM turns WHERE session_id = ? ORDER BY id LIMIT ?", (session_id, limit)
        )
        return [TurnRow(**dict(r)) for r in rows]

    def purge_expired(self, now: datetime | None = None) -> int:
        """Delete turns whose `retain_until` has passed.

        NULL = keep until the session is deleted.
        """
        cur = self._db.execute(
            "DELETE FROM turns WHERE retain_until IS NOT NULL AND retain_until <= ?",
            (_iso(now or _now()),),
        )
        return int(cur.rowcount)


# ---- tasks ---------------------------------------------------------------------------------------


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskRow(Row):
    id: str
    kind: str
    priority: int = 0
    status: TaskStatus = TaskStatus.PENDING
    payload: dict[str, Any] = Field(default_factory=dict)
    checkpoint: dict[str, Any] | None = None
    error: str = ""
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> TaskRow:
        data = dict(row)
        data["payload"] = json.loads(data.pop("payload_json") or "{}")
        raw_checkpoint = data.pop("checkpoint_json")
        data["checkpoint"] = None if raw_checkpoint is None else json.loads(raw_checkpoint)
        return cls(**data)


class TaskRepository:
    """Minimal v0.1 task store. Higher `priority` runs first, ties by creation order."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def add(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        priority: int = 0,
        task_id: str | None = None,
    ) -> TaskRow:
        tid = task_id or str(uuid.uuid4())
        now = _iso(_now())
        self._db.execute(
            "INSERT INTO tasks (id, kind, priority, status, payload_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tid, kind, priority, TaskStatus.PENDING, json.dumps(payload or {}), now, now),
        )
        row = self.get(tid)
        assert row is not None
        return row

    def get(self, task_id: str) -> TaskRow | None:
        row = self._db.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return None if row is None else TaskRow.from_row(row)

    def set_status(self, task_id: str, status: TaskStatus, *, error: str = "") -> bool:
        cur = self._db.execute(
            "UPDATE tasks SET status = ?, error = ?, updated_at = ? WHERE id = ?",
            (status, error, _iso(_now()), task_id),
        )
        return cur.rowcount == 1

    def save_checkpoint(self, task_id: str, checkpoint: dict[str, Any]) -> bool:
        cur = self._db.execute(
            "UPDATE tasks SET checkpoint_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(checkpoint), _iso(_now()), task_id),
        )
        return cur.rowcount == 1

    def next_pending(self) -> TaskRow | None:
        row = self._db.fetch_one(
            "SELECT * FROM tasks WHERE status = ? ORDER BY priority DESC, created_at, id LIMIT 1",
            (TaskStatus.PENDING,),
        )
        return None if row is None else TaskRow.from_row(row)

    def list_by_status(self, *statuses: TaskStatus, limit: int = 100) -> list[TaskRow]:
        wanted = statuses or (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.PAUSED)
        marks = ",".join("?" for _ in wanted)
        rows = self._db.fetch_all(
            f"SELECT * FROM tasks WHERE status IN ({marks}) "  # noqa: S608 - placeholders only
            "ORDER BY priority DESC, created_at, id LIMIT ?",
            (*wanted, limit),
        )
        return [TaskRow.from_row(r) for r in rows]

    def reset_running(self) -> int:
        """After a crash: RUNNING tasks go back to PENDING (their checkpoint is kept)."""
        cur = self._db.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE status = ?",
            (TaskStatus.PENDING, _iso(_now()), TaskStatus.RUNNING),
        )
        return int(cur.rowcount)

    def delete(self, task_id: str) -> bool:
        return self._db.execute("DELETE FROM tasks WHERE id = ?", (task_id,)).rowcount == 1


# ---- temporary grants ----------------------------------------------------------------------------


class TemporaryGrantRow(Row):
    grant_id: str
    agent: str = "*"
    tool: str = "*"
    action_glob: str = "*"
    target_glob: str = "*"
    scope: str = ""
    max_risk: str = "low"
    expires_at: datetime
    origin: str = "user"
    created_at: datetime
    revoked_at: datetime | None = None


class TemporaryGrantRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(
        self,
        *,
        expires_at: datetime,
        grant_id: str | None = None,
        agent: str = "*",
        tool: str = "*",
        action_glob: str = "*",
        target_glob: str = "*",
        scope: str = "",
        max_risk: str = "low",
        origin: str = "user",
    ) -> TemporaryGrantRow:
        gid = grant_id or str(uuid.uuid4())
        self._db.execute(
            "INSERT INTO temporary_grants (grant_id, agent, tool, action_glob, target_glob, scope, "
            "max_risk, expires_at, origin, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                gid,
                agent,
                tool,
                action_glob,
                target_glob,
                scope,
                max_risk,
                _iso(expires_at),
                origin,
                _iso(_now()),
            ),
        )
        row = self.get(gid)
        assert row is not None
        return row

    def get(self, grant_id: str) -> TemporaryGrantRow | None:
        row = self._db.fetch_one("SELECT * FROM temporary_grants WHERE grant_id = ?", (grant_id,))
        return None if row is None else TemporaryGrantRow(**dict(row))

    def list_active(self, now: datetime | None = None) -> list[TemporaryGrantRow]:
        rows = self._db.fetch_all(
            "SELECT * FROM temporary_grants WHERE revoked_at IS NULL AND expires_at > ? "
            "ORDER BY expires_at",
            (_iso(now or _now()),),
        )
        return [TemporaryGrantRow(**dict(r)) for r in rows]

    def revoke(self, grant_id: str, now: datetime | None = None) -> bool:
        cur = self._db.execute(
            "UPDATE temporary_grants SET revoked_at = ? WHERE grant_id = ? AND revoked_at IS NULL",
            (_iso(now or _now()), grant_id),
        )
        return cur.rowcount == 1

    def purge(self, now: datetime | None = None) -> int:
        """Delete expired or revoked grants (their existence is already in the audit log)."""
        cur = self._db.execute(
            "DELETE FROM temporary_grants WHERE expires_at <= ? OR revoked_at IS NOT NULL",
            (_iso(now or _now()),),
        )
        return int(cur.rowcount)


# ---- memory items (ST-07-01, Data Model L2 `memory_items`) ---------------------------------------


class MemoryItemRow(Row):
    id: int
    type: str
    text: str
    importance: float
    source: str = ""
    vault_path: str | None = None
    created_at: datetime
    last_used_at: datetime | None = None
    retain_until: datetime | None = None
    privacy_class: str = "normal"


class MemoryItemRepository:
    """CRUD over `memory_items` (+ FTS5 shadow table `memory_items_fts`, triggers already in
    0001_initial.sql). No privacy/zone decision here - `nox.memory.items.MemoryService` is the
    write-refusal boundary; this repository only persists what it is given."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def add(
        self,
        *,
        type: str,  # noqa: A002 - matches the column name, kept for call-site readability
        text: str,
        importance: float,
        source: str = "",
        vault_path: str | None = None,
        privacy_class: str = "normal",
        retain_until: datetime | None = None,
        created_at: datetime | None = None,
    ) -> MemoryItemRow:
        cur = self._db.execute(
            "INSERT INTO memory_items "
            "(type, text, importance, source, vault_path, created_at, retain_until, privacy_class) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                type,
                text,
                importance,
                source,
                vault_path,
                _iso(created_at or _now()),
                _iso(retain_until),
                privacy_class,
            ),
        )
        row = self.get(int(cur.lastrowid or 0))
        assert row is not None
        return row

    def get(self, item_id: int) -> MemoryItemRow | None:
        row = self._db.fetch_one("SELECT * FROM memory_items WHERE id = ?", (item_id,))
        return None if row is None else MemoryItemRow(**dict(row))

    def touch(self, item_id: int, *, ts: datetime | None = None) -> None:
        self._db.execute(
            "UPDATE memory_items SET last_used_at = ? WHERE id = ?", (_iso(ts or _now()), item_id)
        )

    def delete(self, item_id: int) -> bool:
        return self._db.execute("DELETE FROM memory_items WHERE id = ?", (item_id,)).rowcount == 1

    def delete_by_vault_path(self, vault_path: str) -> int:
        cur = self._db.execute("DELETE FROM memory_items WHERE vault_path = ?", (vault_path,))
        return int(cur.rowcount)

    def search_fts(self, query: str, limit: int = 10) -> list[MemoryItemRow]:
        """FTS5 fallback search (no embeddings available/loaded)."""
        rows = self._db.fetch_all(
            "SELECT m.* FROM memory_items m JOIN memory_items_fts f ON f.rowid = m.id "
            "WHERE memory_items_fts MATCH ? ORDER BY bm25(memory_items_fts) LIMIT ?",
            (query, limit),
        )
        return [MemoryItemRow(**dict(r)) for r in rows]

    def list_by_ids(self, ids: list[int]) -> list[MemoryItemRow]:
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        rows = self._db.fetch_all(
            f"SELECT * FROM memory_items WHERE id IN ({marks})",  # noqa: S608 - placeholders only
            tuple(ids),
        )
        return [MemoryItemRow(**dict(r)) for r in rows]

    def list_expired(self, now: datetime | None = None) -> list[MemoryItemRow]:
        rows = self._db.fetch_all(
            "SELECT * FROM memory_items WHERE retain_until IS NOT NULL AND retain_until <= ?",
            (_iso(now or _now()),),
        )
        return [MemoryItemRow(**dict(r)) for r in rows]

    def purge_expired(self, now: datetime | None = None) -> int:
        cur = self._db.execute(
            "DELETE FROM memory_items WHERE retain_until IS NOT NULL AND retain_until <= ?",
            (_iso(now or _now()),),
        )
        return int(cur.rowcount)

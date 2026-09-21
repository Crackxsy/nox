"""Typed repository over the `clips`/`clip_markers` tables (migration `0006_clips.sql`). Mirrors
`nox.data.stream_repos`'s conventions: frozen pydantic rows, ISO-8601 UTC timestamps, JSON-encoded
list columns."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import Field

from nox.data.db import Database
from nox.data.repos import Row

STATUSES = ("new", "reviewed", "exported", "discarded")


def _iso(value: datetime | None = None) -> str:
    value = value or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


class ClipRow(Row):
    id: str
    source: str
    trigger_kind: str
    origin_event_id: str = ""
    session_id: str = ""
    file_path: str
    duration_s: float = 0.0
    created_at: datetime
    thumbnail_path: str | None = None
    tags: list[str] = Field(default_factory=list)
    status: str = "new"
    parent_clip_id: str | None = None
    checksum: str = ""
    notes: str = ""


class ClipMarkerRow(Row):
    id: str
    session_id: str = ""
    timestamp_s: float
    reason: str = ""
    promoted_clip_id: str | None = None
    created_at: datetime


def _clip_from_row(row: Any) -> ClipRow:
    data = dict(row)
    data["tags"] = json.loads(data.get("tags") or "[]")
    return ClipRow(**data)


class ClipRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def insert(
        self,
        *,
        source: str,
        trigger_kind: str,
        file_path: str,
        origin_event_id: str = "",
        session_id: str = "",
        duration_s: float = 0.0,
        thumbnail_path: str | None = None,
        tags: list[str] | None = None,
        parent_clip_id: str | None = None,
        checksum: str = "",
        notes: str = "",
        clip_id: str | None = None,
    ) -> ClipRow:
        clip_id = clip_id or new_id()
        created_at = _iso()
        self._db.execute(
            "INSERT INTO clips (id, source, trigger_kind, origin_event_id, session_id, "
            "file_path, duration_s, created_at, thumbnail_path, tags, status, parent_clip_id, "
            "checksum, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?)",
            (
                clip_id,
                source,
                trigger_kind,
                origin_event_id,
                session_id,
                file_path,
                duration_s,
                created_at,
                thumbnail_path,
                json.dumps(list(tags or [])),
                parent_clip_id,
                checksum,
                notes,
            ),
        )
        row = self.get(clip_id)
        assert row is not None
        return row

    def get(self, clip_id: str) -> ClipRow | None:
        row = self._db.fetch_one("SELECT * FROM clips WHERE id = ?", (clip_id,))
        return None if row is None else _clip_from_row(row)

    def get_by_checksum(self, checksum: str) -> ClipRow | None:
        if not checksum:
            return None
        row = self._db.fetch_one("SELECT * FROM clips WHERE checksum = ? LIMIT 1", (checksum,))
        return None if row is None else _clip_from_row(row)

    def list_clips(self, *, status: str | None = None, limit: int = 50) -> list[ClipRow]:
        if status is not None:
            rows = self._db.fetch_all(
                "SELECT * FROM clips WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            rows = self._db.fetch_all(
                "SELECT * FROM clips ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        return [_clip_from_row(r) for r in rows]

    def set_status(self, clip_id: str, status: str) -> bool:
        if status not in STATUSES:
            raise ValueError(f"unknown clip status {status!r}")
        cur = self._db.execute("UPDATE clips SET status = ? WHERE id = ?", (status, clip_id))
        return cur.rowcount == 1

    def update_tags(
        self, clip_id: str, *, tags: list[str] | None = None, notes: str | None = None
    ) -> bool:
        row = self.get(clip_id)
        if row is None:
            return False
        new_tags = row.tags if tags is None else list(tags)
        new_notes = row.notes if notes is None else notes
        cur = self._db.execute(
            "UPDATE clips SET tags = ?, notes = ? WHERE id = ?",
            (json.dumps(new_tags), new_notes, clip_id),
        )
        return cur.rowcount == 1

    def add_tags(self, clip_id: str, tags: list[str]) -> bool:
        row = self.get(clip_id)
        if row is None:
            return False
        merged = list(dict.fromkeys([*row.tags, *tags]))  # de-duplicate, keep order
        return self.update_tags(clip_id, tags=merged)


class ClipMarkerRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def insert(
        self, *, session_id: str, timestamp_s: float, reason: str = "", marker_id: str | None = None
    ) -> ClipMarkerRow:
        marker_id = marker_id or new_id()
        created_at = _iso()
        self._db.execute(
            "INSERT INTO clip_markers (id, session_id, timestamp_s, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (marker_id, session_id, timestamp_s, reason, created_at),
        )
        row = self.get(marker_id)
        assert row is not None
        return row

    def get(self, marker_id: str) -> ClipMarkerRow | None:
        row = self._db.fetch_one("SELECT * FROM clip_markers WHERE id = ?", (marker_id,))
        return None if row is None else ClipMarkerRow(**dict(row))

    def list_markers(
        self, *, session_id: str | None = None, limit: int = 100
    ) -> list[ClipMarkerRow]:
        if session_id is not None:
            rows = self._db.fetch_all(
                "SELECT * FROM clip_markers WHERE session_id = ? ORDER BY timestamp_s",
                (session_id,),
            )
        else:
            rows = self._db.fetch_all(
                "SELECT * FROM clip_markers ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        return [ClipMarkerRow(**dict(r)) for r in rows]

    def promote(self, marker_id: str, clip_id: str) -> bool:
        cur = self._db.execute(
            "UPDATE clip_markers SET promoted_clip_id = ? WHERE id = ?", (clip_id, marker_id)
        )
        return cur.rowcount == 1

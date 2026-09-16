"""Typed repositories over the Rocket League Stage 1 tables (Spec v0.3 §7, migration
`0003_rl.sql`). Mirrors `nox.data.stream_repos`'s conventions: frozen pydantic rows, ISO-8601 UTC
timestamps, `purge_expired` for the nightly retention job. `rl_replays` rows are never purged here
(Spec §7: never deleted by Nox except on explicit request) - no retain_until column for that table.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from nox.data.db import Database
from nox.data.repos import Row


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _now() -> datetime:
    return datetime.now(UTC)


# ---- replays -------------------------------------------------------------------------------------


class RlReplayRow(Row):
    id: int
    file_path: str
    file_hash: str = ""
    parsed_at: datetime
    parser_version: str = ""
    parse_status: str = "failed"  # ok | partial | failed
    header_json: str = "{}"
    matched_match_id: int | None = None


class RlReplayRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def upsert(
        self,
        *,
        file_path: str,
        file_hash: str,
        parser_version: str,
        parse_status: str,
        header: dict[str, Any],
        parsed_at: datetime | None = None,
    ) -> RlReplayRow:
        self._db.execute(
            "INSERT INTO rl_replays (file_path, file_hash, parsed_at, parser_version, "
            "parse_status, header_json) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(file_path) DO UPDATE SET file_hash=excluded.file_hash, "
            "parsed_at=excluded.parsed_at, parser_version=excluded.parser_version, "
            "parse_status=excluded.parse_status, header_json=excluded.header_json",
            (
                file_path,
                file_hash,
                _iso(parsed_at or _now()),
                parser_version,
                parse_status,
                json.dumps(header),
            ),
        )
        row = self.get_by_path(file_path)
        assert row is not None
        return row

    def get(self, replay_id: int) -> RlReplayRow | None:
        row = self._db.fetch_one("SELECT * FROM rl_replays WHERE id = ?", (replay_id,))
        return None if row is None else RlReplayRow(**dict(row))

    def get_by_path(self, file_path: str) -> RlReplayRow | None:
        row = self._db.fetch_one("SELECT * FROM rl_replays WHERE file_path = ?", (file_path,))
        return None if row is None else RlReplayRow(**dict(row))

    def set_matched_match(self, replay_id: int, match_id: int | None) -> None:
        self._db.execute(
            "UPDATE rl_replays SET matched_match_id = ? WHERE id = ?", (match_id, replay_id)
        )

    def list_recent(self, limit: int = 20) -> list[RlReplayRow]:
        rows = self._db.fetch_all(
            "SELECT * FROM rl_replays ORDER BY parsed_at DESC LIMIT ?", (limit,)
        )
        return [RlReplayRow(**dict(r)) for r in rows]

    def count_by_status(self) -> dict[str, int]:
        rows = self._db.fetch_all(
            "SELECT parse_status, COUNT(*) AS n FROM rl_replays GROUP BY parse_status"
        )
        return {str(r["parse_status"]): int(r["n"]) for r in rows}


# ---- matches -------------------------------------------------------------------------------------


class RlMatchRow(Row):
    id: int
    started_at: datetime
    ended_at: datetime | None = None
    score_self: int | None = None
    score_opponent: int | None = None
    result: str = "unknown"
    summary_short: str = ""
    summary_detailed: str = ""
    replay_id: int | None = None
    ended_reason: str = "unknown"
    retain_until: datetime | None = None


class RlMatchRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def start(self, *, started_at: datetime | None = None) -> RlMatchRow:
        cur = self._db.execute(
            "INSERT INTO rl_matches (started_at) VALUES (?)", (_iso(started_at or _now()),)
        )
        row = self.get(int(cur.lastrowid or 0))
        assert row is not None
        return row

    def get(self, match_id: int) -> RlMatchRow | None:
        row = self._db.fetch_one("SELECT * FROM rl_matches WHERE id = ?", (match_id,))
        return None if row is None else RlMatchRow(**dict(row))

    def end(
        self,
        match_id: int,
        *,
        score_self: int | None = None,
        score_opponent: int | None = None,
        result: str = "unknown",
        summary_short: str = "",
        ended_reason: str = "normal",
        ended_at: datetime | None = None,
    ) -> bool:
        cur = self._db.execute(
            "UPDATE rl_matches SET ended_at = ?, score_self = ?, score_opponent = ?, result = ?, "
            "summary_short = ?, ended_reason = ? WHERE id = ? AND ended_at IS NULL",
            (
                _iso(ended_at or _now()),
                score_self,
                score_opponent,
                result,
                summary_short,
                ended_reason,
                match_id,
            ),
        )
        return cur.rowcount == 1

    def set_detailed_summary(self, match_id: int, summary_detailed: str) -> None:
        self._db.execute(
            "UPDATE rl_matches SET summary_detailed = ? WHERE id = ?", (summary_detailed, match_id)
        )

    def set_replay(self, match_id: int, replay_id: int | None) -> None:
        self._db.execute("UPDATE rl_matches SET replay_id = ? WHERE id = ?", (replay_id, match_id))

    def active(self) -> RlMatchRow | None:
        row = self._db.fetch_one(
            "SELECT * FROM rl_matches WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
        )
        return None if row is None else RlMatchRow(**dict(row))

    def list_since(self, started_after: datetime) -> list[RlMatchRow]:
        rows = self._db.fetch_all(
            "SELECT * FROM rl_matches WHERE started_at >= ? ORDER BY started_at",
            (_iso(started_after),),
        )
        return [RlMatchRow(**dict(r)) for r in rows]

    def list_recent(self, limit: int = 20) -> list[RlMatchRow]:
        rows = self._db.fetch_all(
            "SELECT * FROM rl_matches ORDER BY started_at DESC LIMIT ?", (limit,)
        )
        return [RlMatchRow(**dict(r)) for r in rows]


# ---- events --------------------------------------------------------------------------------------


class RlEventRow(Row):
    id: int
    match_id: int | None = None
    ts: datetime
    kind: str
    source: str
    confidence: float = 0.0
    payload_json: str = "{}"
    retain_until: datetime | None = None


class RlEventRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(
        self,
        *,
        match_id: int | None,
        kind: str,
        source: str,
        confidence: float = 0.0,
        payload: dict[str, Any] | None = None,
        ts: datetime | None = None,
        retain_until: datetime | None = None,
    ) -> RlEventRow:
        cur = self._db.execute(
            "INSERT INTO rl_events (match_id, ts, kind, source, confidence, payload_json, "
            "retain_until) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                match_id,
                _iso(ts or _now()),
                kind,
                source,
                confidence,
                json.dumps(payload or {}),
                _iso(retain_until),
            ),
        )
        row = self.get(int(cur.lastrowid or 0))
        assert row is not None
        return row

    def get(self, event_id: int) -> RlEventRow | None:
        row = self._db.fetch_one("SELECT * FROM rl_events WHERE id = ?", (event_id,))
        return None if row is None else RlEventRow(**dict(row))

    def list_for_match(self, match_id: int) -> list[RlEventRow]:
        rows = self._db.fetch_all(
            "SELECT * FROM rl_events WHERE match_id = ? ORDER BY ts", (match_id,)
        )
        return [RlEventRow(**dict(r)) for r in rows]

    def list_recent(self, limit: int = 50) -> list[RlEventRow]:
        rows = self._db.fetch_all("SELECT * FROM rl_events ORDER BY ts DESC LIMIT ?", (limit,))
        return [RlEventRow(**dict(r)) for r in rows]

    def purge_expired(self, *, now: datetime | None = None) -> int:
        cur = self._db.execute(
            "DELETE FROM rl_events WHERE retain_until IS NOT NULL AND retain_until < ?",
            (_iso(now or _now()),),
        )
        return cur.rowcount


def default_retain_until(days: int, *, now: datetime | None = None) -> datetime | None:
    if days <= 0:
        return None
    from datetime import timedelta

    return (now or _now()) + timedelta(days=days)

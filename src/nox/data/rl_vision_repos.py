"""Typed repositories over the Vision Stage 2 tables (Spec v0.9 §7, migration `0009_vision.sql`).
Mirrors `nox.data.rl_repos`'s conventions: frozen pydantic rows, ISO-8601 UTC timestamps,
`purge_expired` for the nightly retention job. `rl_vision_frames` is short-retention by design
(`rl.vision.detections_retain_hours`, Spec §7 open point's proposed minimization default); the
per-match `rl_vision_analysis` row it feeds is the durable artifact."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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


def default_retain_until_hours(hours: float, *, now: datetime | None = None) -> datetime | None:
    if hours <= 0:
        return None
    return (now or _now()) + timedelta(hours=hours)


# ---- frames ----------------------------------------------------------------------------------


class RlVisionFrameRow(Row):
    id: int
    match_id: int | None = None
    ts: datetime
    entity: str  # ball | car
    confidence: float = 0.0
    x: float
    y: float
    w: float
    h: float
    team: str | None = None
    backend: str = "none"
    retain_until: datetime | None = None


class RlVisionFrameRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(
        self,
        *,
        match_id: int | None,
        entity: str,
        confidence: float,
        x: float,
        y: float,
        w: float,
        h: float,
        team: str | None = None,
        backend: str = "none",
        ts: datetime | None = None,
        retain_until: datetime | None = None,
    ) -> RlVisionFrameRow:
        cur = self._db.execute(
            "INSERT INTO rl_vision_frames (match_id, ts, entity, confidence, x, y, w, h, team, "
            "backend, retain_until) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                match_id,
                _iso(ts or _now()),
                entity,
                confidence,
                x,
                y,
                w,
                h,
                team,
                backend,
                _iso(retain_until),
            ),
        )
        row = self.get(int(cur.lastrowid or 0))
        assert row is not None
        return row

    def get(self, frame_id: int) -> RlVisionFrameRow | None:
        row = self._db.fetch_one("SELECT * FROM rl_vision_frames WHERE id = ?", (frame_id,))
        return None if row is None else RlVisionFrameRow(**dict(row))

    def list_for_match(self, match_id: int) -> list[RlVisionFrameRow]:
        rows = self._db.fetch_all(
            "SELECT * FROM rl_vision_frames WHERE match_id = ? ORDER BY ts", (match_id,)
        )
        return [RlVisionFrameRow(**dict(r)) for r in rows]

    def purge_expired(self, *, now: datetime | None = None) -> int:
        cur = self._db.execute(
            "DELETE FROM rl_vision_frames WHERE retain_until IS NOT NULL AND retain_until < ?",
            (_iso(now or _now()),),
        )
        return cur.rowcount


# ---- analysis --------------------------------------------------------------------------------


class RlVisionAnalysisRow(Row):
    id: int
    match_id: int | None = None
    analyzed_at: datetime
    frames_analyzed: int = 0
    ball_side_ratio: float | None = None
    avg_self_car_ball_distance: float | None = None
    coaching_summary: str = ""


class RlVisionAnalysisRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(
        self,
        *,
        match_id: int | None,
        frames_analyzed: int,
        ball_side_ratio: float | None,
        avg_self_car_ball_distance: float | None,
        coaching_summary: str,
        analyzed_at: datetime | None = None,
    ) -> RlVisionAnalysisRow:
        cur = self._db.execute(
            "INSERT INTO rl_vision_analysis (match_id, analyzed_at, frames_analyzed, "
            "ball_side_ratio, avg_self_car_ball_distance, coaching_summary) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                match_id,
                _iso(analyzed_at or _now()),
                frames_analyzed,
                ball_side_ratio,
                avg_self_car_ball_distance,
                coaching_summary,
            ),
        )
        row = self.get(int(cur.lastrowid or 0))
        assert row is not None
        return row

    def get(self, analysis_id: int) -> RlVisionAnalysisRow | None:
        row = self._db.fetch_one("SELECT * FROM rl_vision_analysis WHERE id = ?", (analysis_id,))
        return None if row is None else RlVisionAnalysisRow(**dict(row))

    def list_for_match(self, match_id: int) -> list[RlVisionAnalysisRow]:
        rows = self._db.fetch_all(
            "SELECT * FROM rl_vision_analysis WHERE match_id = ? ORDER BY analyzed_at", (match_id,)
        )
        return [RlVisionAnalysisRow(**dict(r)) for r in rows]

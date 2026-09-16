"""Typed repositories over the Stream Bot tables (Spec v0.2 §7, migration `0002_stream.sql`).

Mirrors the conventions of `nox.data.repos`: rows are frozen pydantic models, timestamps are
ISO-8601 UTC text, and every repository that holds retention-bound data exposes `purge_expired`
for the nightly retention job (Data Model "Retention"). `text`/`what` content is never logged by
callers of these repositories - only ids and counts belong in audit details.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
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


# ---- stream sessions -------------------------------------------------------------------------


class StreamSessionRow(Row):
    id: int
    started_at: datetime
    ended_at: datetime | None = None
    mode: str = "live"
    preflight_json: str = "{}"
    peak_viewers: int = 0
    chat_message_count: int = 0
    funken_awarded_total: float = 0.0
    summary: str = ""
    ended_reason: str | None = None


class StreamSessionRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def start(
        self,
        *,
        mode: str = "live",
        preflight: dict[str, Any] | None = None,
        started_at: datetime | None = None,
    ) -> StreamSessionRow:
        cur = self._db.execute(
            "INSERT INTO stream_sessions (started_at, mode, preflight_json) VALUES (?, ?, ?)",
            (_iso(started_at or _now()), mode, json.dumps(preflight or {})),
        )
        row = self.get(int(cur.lastrowid or 0))
        assert row is not None
        return row

    def get(self, session_id: int) -> StreamSessionRow | None:
        row = self._db.fetch_one("SELECT * FROM stream_sessions WHERE id = ?", (session_id,))
        return None if row is None else StreamSessionRow(**dict(row))

    def active(self) -> StreamSessionRow | None:
        row = self._db.fetch_one(
            "SELECT * FROM stream_sessions WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
        )
        return None if row is None else StreamSessionRow(**dict(row))

    def end(
        self,
        session_id: int,
        *,
        ended_reason: str = "manual",
        summary: str = "",
        ended_at: datetime | None = None,
    ) -> bool:
        cur = self._db.execute(
            "UPDATE stream_sessions SET ended_at = ?, ended_reason = ?, summary = ? "
            "WHERE id = ? AND ended_at IS NULL",
            (_iso(ended_at or _now()), ended_reason, summary, session_id),
        )
        return cur.rowcount == 1

    def record_chat_message(self, session_id: int) -> None:
        self._db.execute(
            "UPDATE stream_sessions SET chat_message_count = chat_message_count + 1 WHERE id = ?",
            (session_id,),
        )

    def record_funken_awarded(self, session_id: int, delta: float) -> None:
        self._db.execute(
            "UPDATE stream_sessions SET funken_awarded_total = funken_awarded_total + ? "
            "WHERE id = ?",
            (delta, session_id),
        )

    def bump_peak_viewers(self, session_id: int, viewers: int) -> None:
        self._db.execute(
            "UPDATE stream_sessions SET peak_viewers = MAX(peak_viewers, ?) WHERE id = ?",
            (viewers, session_id),
        )

    def list_recent(self, limit: int = 20) -> list[StreamSessionRow]:
        rows = self._db.fetch_all(
            "SELECT * FROM stream_sessions ORDER BY started_at DESC LIMIT ?", (limit,)
        )
        return [StreamSessionRow(**dict(r)) for r in rows]


# ---- viewers ---------------------------------------------------------------------------------


class ViewerRow(Row):
    twitch_user_id: str
    display_name: str = ""
    first_seen_at: datetime
    last_seen_at: datetime
    loyalty_tier: str = "none"
    funken_balance: float = 0.0
    opt_out: bool = False
    retain_until: datetime | None = None

    @classmethod
    def from_row(cls, row: Any) -> ViewerRow:
        data = dict(row)
        data["opt_out"] = bool(data["opt_out"])
        return cls(**data)


class ViewerRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def touch(
        self,
        twitch_user_id: str,
        display_name: str = "",
        *,
        seen_at: datetime | None = None,
        retain_until: datetime | None = None,
    ) -> ViewerRow:
        """Upsert: create on first sight, otherwise bump `last_seen_at`/`display_name`."""
        now = seen_at or _now()
        existing = self.get(twitch_user_id)
        if existing is None:
            self._db.execute(
                "INSERT INTO viewers (twitch_user_id, display_name, first_seen_at, last_seen_at, "
                "retain_until) VALUES (?, ?, ?, ?, ?)",
                (twitch_user_id, display_name, _iso(now), _iso(now), _iso(retain_until)),
            )
        else:
            self._db.execute(
                "UPDATE viewers SET display_name = ?, last_seen_at = ?, retain_until = ? "
                "WHERE twitch_user_id = ?",
                (
                    display_name or existing.display_name,
                    _iso(now),
                    _iso(retain_until) if retain_until is not None else _iso(existing.retain_until),
                    twitch_user_id,
                ),
            )
        row = self.get(twitch_user_id)
        assert row is not None
        return row

    def get(self, twitch_user_id: str) -> ViewerRow | None:
        row = self._db.fetch_one(
            "SELECT * FROM viewers WHERE twitch_user_id = ?", (twitch_user_id,)
        )
        return None if row is None else ViewerRow.from_row(row)

    def set_balance(self, twitch_user_id: str, balance: float) -> None:
        self._db.execute(
            "UPDATE viewers SET funken_balance = ? WHERE twitch_user_id = ?",
            (balance, twitch_user_id),
        )

    def set_tier(self, twitch_user_id: str, tier: str) -> None:
        self._db.execute(
            "UPDATE viewers SET loyalty_tier = ? WHERE twitch_user_id = ?",
            (tier, twitch_user_id),
        )

    def set_opt_out(self, twitch_user_id: str, opt_out: bool) -> bool:
        cur = self._db.execute(
            "UPDATE viewers SET opt_out = ? WHERE twitch_user_id = ?",
            (int(opt_out), twitch_user_id),
        )
        return cur.rowcount == 1

    def delete(self, twitch_user_id: str) -> bool:
        """Viewer-requested erasure (FR-7.9/FR-9.15): cascades `viewer_memory`/`funken_ledger`;
        `chat_events`/`moderation_actions` keep their rows with `viewer_id` set to NULL."""
        cur = self._db.execute("DELETE FROM viewers WHERE twitch_user_id = ?", (twitch_user_id,))
        return cur.rowcount == 1

    def list_inactive_before(self, cutoff: datetime) -> list[ViewerRow]:
        rows = self._db.fetch_all(
            "SELECT * FROM viewers WHERE last_seen_at < ? ORDER BY last_seen_at", (_iso(cutoff),)
        )
        return [ViewerRow.from_row(r) for r in rows]

    def list_top_by_balance(self, limit: int = 10) -> list[ViewerRow]:
        """Additive (Stream Bot core, EPIC-11 ST-11-09): backs the `stream.funken.top` IPC
        request/dashboard leaderboard. Opted-out viewers are excluded (FR-9.15)."""
        rows = self._db.fetch_all(
            "SELECT * FROM viewers WHERE opt_out = 0 ORDER BY funken_balance DESC, "
            "twitch_user_id LIMIT ?",
            (limit,),
        )
        return [ViewerRow.from_row(r) for r in rows]

    def purge_expired(self, now: datetime | None = None) -> int:
        cutoff = _iso(now or _now())
        cur = self._db.execute(
            "DELETE FROM viewers WHERE retain_until IS NOT NULL AND retain_until <= ?", (cutoff,)
        )
        return int(cur.rowcount)


# ---- viewer memory ----------------------------------------------------------------------------


class ViewerMemoryRow(Row):
    id: int
    viewer_id: str
    what: str
    source_event_id: int | None = None
    confidence: float = 0.5
    created_at: datetime
    retain_until: datetime | None = None


class ViewerMemoryRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(
        self,
        viewer_id: str,
        what: str,
        *,
        source_event_id: int | None = None,
        confidence: float = 0.5,
        created_at: datetime | None = None,
        retain_until: datetime | None = None,
    ) -> int:
        cur = self._db.execute(
            "INSERT INTO viewer_memory (viewer_id, what, source_event_id, confidence, created_at, "
            "retain_until) VALUES (?, ?, ?, ?, ?, ?)",
            (
                viewer_id,
                what,
                source_event_id,
                confidence,
                _iso(created_at or _now()),
                _iso(retain_until),
            ),
        )
        return int(cur.lastrowid or 0)

    def list_for_viewer(self, viewer_id: str, limit: int = 200) -> list[ViewerMemoryRow]:
        rows = self._db.fetch_all(
            "SELECT * FROM viewer_memory WHERE viewer_id = ? ORDER BY created_at LIMIT ?",
            (viewer_id, limit),
        )
        return [ViewerMemoryRow(**dict(r)) for r in rows]

    def delete_for_viewer(self, viewer_id: str) -> int:
        cur = self._db.execute("DELETE FROM viewer_memory WHERE viewer_id = ?", (viewer_id,))
        return int(cur.rowcount)

    def purge_expired(self, now: datetime | None = None) -> int:
        cur = self._db.execute(
            "DELETE FROM viewer_memory WHERE retain_until IS NOT NULL AND retain_until <= ?",
            (_iso(now or _now()),),
        )
        return int(cur.rowcount)


# ---- chat events -------------------------------------------------------------------------------


class ChatEventRow(Row):
    id: int
    session_id: int | None = None
    ts: datetime
    viewer_id: str | None = None
    kind: str = "message"
    text: str = ""
    priority: int = 0
    handled_by: str = ""
    decision: str = ""
    retain_until: datetime | None = None


class ChatEventRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(
        self,
        *,
        session_id: int | None,
        kind: str = "message",
        viewer_id: str | None = None,
        text: str = "",
        priority: int = 0,
        handled_by: str = "",
        decision: str = "",
        ts: datetime | None = None,
        retain_until: datetime | None = None,
    ) -> int:
        cur = self._db.execute(
            "INSERT INTO chat_events (session_id, ts, viewer_id, kind, text, priority, "
            "handled_by, decision, retain_until) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                _iso(ts or _now()),
                viewer_id,
                kind,
                text,
                priority,
                handled_by,
                decision,
                _iso(retain_until),
            ),
        )
        return int(cur.lastrowid or 0)

    def get(self, chat_event_id: int) -> ChatEventRow | None:
        row = self._db.fetch_one("SELECT * FROM chat_events WHERE id = ?", (chat_event_id,))
        return None if row is None else ChatEventRow(**dict(row))

    def set_handling(self, chat_event_id: int, *, handled_by: str, decision: str = "") -> bool:
        cur = self._db.execute(
            "UPDATE chat_events SET handled_by = ?, decision = ? WHERE id = ?",
            (handled_by, decision, chat_event_id),
        )
        return cur.rowcount == 1

    def list_for_session(self, session_id: int, limit: int = 1000) -> list[ChatEventRow]:
        rows = self._db.fetch_all(
            "SELECT * FROM chat_events WHERE session_id = ? ORDER BY id LIMIT ?",
            (session_id, limit),
        )
        return [ChatEventRow(**dict(r)) for r in rows]

    def purge_expired(self, now: datetime | None = None) -> int:
        cur = self._db.execute(
            "DELETE FROM chat_events WHERE retain_until IS NOT NULL AND retain_until <= ?",
            (_iso(now or _now()),),
        )
        return int(cur.rowcount)


# ---- funken ledger -----------------------------------------------------------------------------


class FunkenLedgerRow(Row):
    id: int
    viewer_id: str
    delta: float
    reason: str = ""
    balance_after: float
    ts: datetime
    source: str = "earn"  # earn | spend | admin | decay


class FunkenLedgerRepository:
    """Append-only (Spec v0.2 §7 `funken_ledger`); there is no update/delete method on purpose."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def append(
        self,
        viewer_id: str,
        delta: float,
        *,
        balance_after: float,
        reason: str = "",
        source: str = "earn",
        ts: datetime | None = None,
    ) -> int:
        cur = self._db.execute(
            "INSERT INTO funken_ledger (viewer_id, delta, reason, balance_after, ts, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (viewer_id, delta, reason, balance_after, _iso(ts or _now()), source),
        )
        return int(cur.lastrowid or 0)

    def list_for_viewer(self, viewer_id: str, limit: int = 200) -> list[FunkenLedgerRow]:
        rows = self._db.fetch_all(
            "SELECT * FROM funken_ledger WHERE viewer_id = ? ORDER BY id DESC LIMIT ?",
            (viewer_id, limit),
        )
        return [FunkenLedgerRow(**dict(r)) for r in rows]

    def last_for_viewer(self, viewer_id: str) -> FunkenLedgerRow | None:
        row = self._db.fetch_one(
            "SELECT * FROM funken_ledger WHERE viewer_id = ? ORDER BY id DESC LIMIT 1",
            (viewer_id,),
        )
        return None if row is None else FunkenLedgerRow(**dict(row))


# ---- moderation actions -------------------------------------------------------------------------


class ModerationActionRow(Row):
    id: int
    ts: datetime
    viewer_id: str | None = None
    chat_event_id: int | None = None
    stage: str
    hard_list_hit: bool = False
    confirmed_by: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> ModerationActionRow:
        data = dict(row)
        data["hard_list_hit"] = bool(data["hard_list_hit"])
        return cls(**data)


class ModerationActionRepository:
    """Audit trail of every moderation decision, including `ignore` (Spec v0.2 §7); never purged
    by the retention job - moderation history outlives the chat text it was decided on."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def add(
        self,
        *,
        stage: str,
        viewer_id: str | None = None,
        chat_event_id: int | None = None,
        hard_list_hit: bool = False,
        confirmed_by: str | None = None,
        ts: datetime | None = None,
    ) -> int:
        cur = self._db.execute(
            "INSERT INTO moderation_actions (ts, viewer_id, chat_event_id, stage, hard_list_hit, "
            "confirmed_by) VALUES (?, ?, ?, ?, ?, ?)",
            (_iso(ts or _now()), viewer_id, chat_event_id, stage, int(hard_list_hit), confirmed_by),
        )
        return int(cur.lastrowid or 0)

    def list_for_viewer(self, viewer_id: str, limit: int = 200) -> list[ModerationActionRow]:
        rows = self._db.fetch_all(
            "SELECT * FROM moderation_actions WHERE viewer_id = ? ORDER BY id DESC LIMIT ?",
            (viewer_id, limit),
        )
        return [ModerationActionRow.from_row(r) for r in rows]


# ---- minigame sessions (reserved, pending OP-B) ------------------------------------------------


class MinigameSessionRow(Row):
    id: int
    session_id: int | None = None
    game_id: str
    started_at: datetime
    ended_at: datetime | None = None
    participants_json: str = "[]"
    result_json: str = "{}"


class MinigameSessionRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def start(
        self,
        game_id: str,
        *,
        session_id: int | None = None,
        participants: list[str] | None = None,
        started_at: datetime | None = None,
    ) -> int:
        cur = self._db.execute(
            "INSERT INTO minigame_sessions (session_id, game_id, started_at, participants_json) "
            "VALUES (?, ?, ?, ?)",
            (
                session_id,
                game_id,
                _iso(started_at or _now()),
                json.dumps(list(participants or [])),
            ),
        )
        return int(cur.lastrowid or 0)

    def end(
        self, minigame_id: int, *, result: dict[str, Any], ended_at: datetime | None = None
    ) -> bool:
        cur = self._db.execute(
            "UPDATE minigame_sessions SET ended_at = ?, result_json = ? WHERE id = ? "
            "AND ended_at IS NULL",
            (_iso(ended_at or _now()), json.dumps(result), minigame_id),
        )
        return cur.rowcount == 1

    def get(self, minigame_id: int) -> MinigameSessionRow | None:
        row = self._db.fetch_one("SELECT * FROM minigame_sessions WHERE id = ?", (minigame_id,))
        return None if row is None else MinigameSessionRow(**dict(row))


def purge_all_expired(
    viewers: ViewerRepository,
    viewer_memory: ViewerMemoryRepository,
    chat_events: ChatEventRepository,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Convenience for the nightly retention job: purge every Stream Bot table with a
    `retain_until` column and report how many rows each one removed."""
    ts = now or _now()
    return {
        "chat_events": chat_events.purge_expired(ts),
        "viewer_memory": viewer_memory.purge_expired(ts),
        "viewers": viewers.purge_expired(ts),
    }


def default_chat_retain_until(days: int, *, now: datetime | None = None) -> datetime:
    """`retain_until` for a new chat event given `stream.chat.retain_raw_text_days`."""
    return (now or _now()) + timedelta(days=days)


def default_viewer_retain_until(months: int, *, now: datetime | None = None) -> datetime:
    """`retain_until` for a viewer given `privacy.retention.viewer_data_inactive_months`."""
    return (now or _now()) + timedelta(days=30 * months)

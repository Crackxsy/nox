"""nox.data.stream_repos: typed rows, retention/purge rules (Spec v0.2 §7)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nox.data.db import Database
from nox.data.stream_repos import (
    ChatEventRepository,
    FunkenLedgerRepository,
    MinigameSessionRepository,
    ModerationActionRepository,
    StreamSessionRepository,
    ViewerMemoryRepository,
    ViewerRepository,
    default_chat_retain_until,
    default_viewer_retain_until,
    purge_all_expired,
)

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "nox.db")
    database.migrate()
    yield database
    database.close()


def test_stream_session_lifecycle(db: Database) -> None:
    repo = StreamSessionRepository(db)
    assert repo.active() is None
    session = repo.start(mode="live", preflight={"obs": "green"}, started_at=NOW)
    assert repo.active() == session
    repo.record_chat_message(session.id)
    repo.record_chat_message(session.id)
    repo.record_funken_awarded(session.id, 2.5)
    repo.bump_peak_viewers(session.id, 10)
    repo.bump_peak_viewers(session.id, 3)  # lower peak must not shrink it
    updated = repo.get(session.id)
    assert updated is not None
    assert updated.chat_message_count == 2
    assert updated.funken_awarded_total == 2.5
    assert updated.peak_viewers == 10

    assert repo.end(session.id, ended_reason="manual", summary="gg", ended_at=NOW) is True
    assert repo.end(session.id) is False  # already ended
    assert repo.active() is None
    ended = repo.get(session.id)
    assert ended is not None and ended.ended_reason == "manual" and ended.summary == "gg"
    assert repo.list_recent(5) == [ended]


def test_viewer_touch_upsert_and_opt_out(db: Database) -> None:
    repo = ViewerRepository(db)
    assert repo.get("v1") is None
    first = repo.touch("v1", "Alice", seen_at=NOW)
    assert first.display_name == "Alice"
    assert first.first_seen_at == NOW and first.last_seen_at == NOW
    assert first.opt_out is False

    later = NOW + timedelta(hours=1)
    updated = repo.touch("v1", "AliceNew", seen_at=later)
    assert updated.first_seen_at == NOW  # unchanged
    assert updated.last_seen_at == later
    assert updated.display_name == "AliceNew"

    repo.set_balance("v1", 42.0)
    repo.set_tier("v1", "silver")
    assert repo.get("v1") is not None
    assert repo.get("v1").funken_balance == 42.0  # type: ignore[union-attr]
    assert repo.get("v1").loyalty_tier == "silver"  # type: ignore[union-attr]

    assert repo.set_opt_out("v1", True) is True
    assert repo.get("v1").opt_out is True  # type: ignore[union-attr]
    assert repo.set_opt_out("no-such-viewer", True) is False


def test_viewer_delete_is_the_erasure_path(db: Database) -> None:
    viewers = ViewerRepository(db)
    memory = ViewerMemoryRepository(db)
    ledger = FunkenLedgerRepository(db)
    chats = ChatEventRepository(db)
    moderation = ModerationActionRepository(db)

    viewers.touch("v1", "Alice", seen_at=NOW)
    memory.add("v1", "likes speedrunning", created_at=NOW)
    ledger.append("v1", 5.0, balance_after=5.0, source="earn", ts=NOW)
    chat_id = chats.add(session_id=None, viewer_id="v1", text="hi", ts=NOW)
    moderation.add(stage="ignore", viewer_id="v1", chat_event_id=chat_id, ts=NOW)

    assert viewers.delete("v1") is True
    assert viewers.get("v1") is None
    assert memory.list_for_viewer("v1") == []  # cascaded
    assert ledger.list_for_viewer("v1") == []  # cascaded
    # chat_events/moderation_actions keep their audit trail with viewer_id set to NULL
    chat_row = chats.get(chat_id)
    assert chat_row is not None and chat_row.viewer_id is None
    mod_rows = db.fetch_all("SELECT viewer_id FROM moderation_actions")
    assert mod_rows[0]["viewer_id"] is None


def test_viewer_retention_purge(db: Database) -> None:
    viewers = ViewerRepository(db)
    viewers.touch("stale", "Old", seen_at=NOW, retain_until=NOW - timedelta(days=1))
    viewers.touch("fresh", "New", seen_at=NOW, retain_until=NOW + timedelta(days=1))
    assert viewers.purge_expired(now=NOW) == 1
    assert viewers.get("stale") is None
    assert viewers.get("fresh") is not None
    inactive = viewers.list_inactive_before(NOW + timedelta(days=2))
    assert [v.twitch_user_id for v in inactive] == ["fresh"]


def test_viewer_memory_add_list_purge(db: Database) -> None:
    ViewerRepository(db).touch("v1", "Alice", seen_at=NOW)
    repo = ViewerMemoryRepository(db)
    repo.add("v1", "prefers German replies", confidence=0.9, created_at=NOW)
    repo.add("v1", "asked about the schedule", created_at=NOW, retain_until=NOW - timedelta(days=1))
    rows = repo.list_for_viewer("v1")
    assert len(rows) == 2
    assert rows[0].what == "prefers German replies"
    assert repo.purge_expired(now=NOW) == 1
    assert len(repo.list_for_viewer("v1")) == 1
    assert repo.delete_for_viewer("v1") == 1
    assert repo.list_for_viewer("v1") == []


def test_chat_events_retention_and_handling(db: Database) -> None:
    session = StreamSessionRepository(db).start(started_at=NOW)
    repo = ChatEventRepository(db)
    keep = repo.add(
        session_id=session.id, text="hello chat", ts=NOW, retain_until=NOW + timedelta(days=7)
    )
    expire = repo.add(
        session_id=session.id, text="spam", ts=NOW, retain_until=NOW - timedelta(seconds=1)
    )
    assert [r.id for r in repo.list_for_session(session.id)] == [keep, expire]
    assert repo.set_handling(keep, handled_by="relevance", decision="reply") is True
    assert repo.get(keep).handled_by == "relevance"  # type: ignore[union-attr]
    assert repo.purge_expired(now=NOW) == 1
    assert [r.id for r in repo.list_for_session(session.id)] == [keep]


def test_funken_ledger_is_append_only_and_ordered(db: Database) -> None:
    ViewerRepository(db).touch("v1", "Alice", seen_at=NOW)
    repo = FunkenLedgerRepository(db)
    repo.append("v1", 1.0, balance_after=1.0, source="earn", ts=NOW)
    repo.append("v1", 2.0, balance_after=3.0, source="earn", ts=NOW + timedelta(seconds=1))
    repo.append("v1", -1.0, balance_after=2.0, source="spend", ts=NOW + timedelta(seconds=2))
    rows = repo.list_for_viewer("v1")
    assert [r.balance_after for r in rows] == [2.0, 3.0, 1.0]  # newest first
    last = repo.last_for_viewer("v1")
    assert last is not None and last.balance_after == 2.0
    assert not hasattr(repo, "update") and not hasattr(repo, "delete")


def test_moderation_actions_list_for_viewer(db: Database) -> None:
    ViewerRepository(db).touch("v1", "Alice", seen_at=NOW)
    repo = ModerationActionRepository(db)
    repo.add(stage="ignore", viewer_id="v1", ts=NOW)
    repo.add(stage="moderate", viewer_id="v1", hard_list_hit=True, confirmed_by="admin", ts=NOW)
    rows = repo.list_for_viewer("v1")
    assert [r.stage for r in rows] == ["moderate", "ignore"]
    assert rows[0].hard_list_hit is True
    assert rows[0].confirmed_by == "admin"


def test_minigame_sessions_reserved(db: Database) -> None:
    session = StreamSessionRepository(db).start(started_at=NOW)
    repo = MinigameSessionRepository(db)
    minigame_id = repo.start(
        "rock_paper_scissors", session_id=session.id, participants=["v1"], started_at=NOW
    )
    row = repo.get(minigame_id)
    assert row is not None and row.game_id == "rock_paper_scissors" and row.ended_at is None
    assert repo.end(minigame_id, result={"winner": "v1"}, ended_at=NOW) is True
    ended = repo.get(minigame_id)
    assert ended is not None and ended.ended_at == NOW


def test_purge_all_expired_aggregates_counts(db: Database) -> None:
    viewers = ViewerRepository(db)
    memory = ViewerMemoryRepository(db)
    chats = ChatEventRepository(db)
    viewers.touch("stale", "Old", seen_at=NOW, retain_until=NOW - timedelta(days=1))
    memory.add("stale", "old fact", created_at=NOW, retain_until=NOW - timedelta(days=1))
    chats.add(session_id=None, text="old chat", ts=NOW, retain_until=NOW - timedelta(days=1))
    counts = purge_all_expired(viewers, memory, chats, now=NOW)
    assert counts == {"chat_events": 1, "viewer_memory": 1, "viewers": 1}


def test_default_retain_until_helpers() -> None:
    assert default_chat_retain_until(7, now=NOW) == NOW + timedelta(days=7)
    assert default_viewer_retain_until(12, now=NOW) == NOW + timedelta(days=360)

"""nox.data.repos: typed rows, retention/purge rules, ordering."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nox.data.db import Database
from nox.data.repos import (
    HealthHistoryRepository,
    NotificationRepository,
    SessionRepository,
    StateCheckpointRepository,
    TaskRepository,
    TaskStatus,
    TemporaryGrantRepository,
    TurnRepository,
)

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "nox.db")
    database.migrate()
    yield database
    database.close()


def test_state_checkpoints_save_latest_prune(db: Database) -> None:
    repo = StateCheckpointRepository(db, keep=5)
    assert repo.latest() is None
    for v in range(1, 8):
        repo.save(v, f"r{v}", f'{{"version": {v}}}')
    latest = repo.latest()
    assert latest is not None and latest.version == 7 and latest.reason == "r7"
    assert latest.state_json == '{"version": 7}'
    assert latest.ts.tzinfo is not None
    assert repo.count() == 5
    assert repo.prune(2) == 3
    assert repo.count() == 2


def test_health_history(db: Database) -> None:
    repo = HealthHistoryRepository(db)
    repo.add("ollama", "available", ts=NOW - timedelta(days=40))
    repo.add("ollama", "unavailable", "down", ts=NOW - timedelta(hours=1))
    repo.add("stt", "limited", "cpu", ts=NOW)
    latest = repo.latest_per_component()
    assert latest["ollama"].status == "unavailable" and latest["stt"].reason == "cpu"
    assert [r.component for r in repo.list_recent(2)] == ["stt", "ollama"]
    assert repo.purge_older_than(30, now=NOW) == 1
    assert len(repo.list_recent(component="ollama")) == 1


def test_sessions_and_turns_with_retention(db: Database) -> None:
    sessions = SessionRepository(db)
    turns = TurnRepository(db)
    s = sessions.create("companion", "balanced", started_at=NOW)
    assert sessions.get(s.id) == s and sessions.active() == s
    keep = turns.add(s.id, "user", "hallo", ts=NOW)
    expire = turns.add(
        s.id,
        "assistant",
        "hi",
        provider="ollama",
        latency_ms=12,
        retain_until=NOW + timedelta(days=1),
    )
    rows = turns.list_for_session(s.id)
    assert [r.id for r in rows] == [keep, expire]
    assert rows[1].provider == "ollama" and rows[1].latency_ms == 12
    assert rows[1].retain_until == NOW + timedelta(days=1)
    assert turns.purge_expired(now=NOW) == 0
    assert turns.purge_expired(now=NOW + timedelta(days=2)) == 1
    assert [r.id for r in turns.list_for_session(s.id)] == [keep]
    assert turns.get(expire) is None
    assert sessions.end(s.id, "kurz", ended_at=NOW + timedelta(minutes=5)) is True
    assert sessions.end(s.id) is False  # already ended
    ended = sessions.get(s.id)
    assert ended is not None and ended.summary == "kurz" and ended.ended_at is not None
    assert sessions.active() is None
    assert [x.id for x in sessions.list_recent()] == [s.id]


def test_tasks_minimal(db: Database) -> None:
    repo = TaskRepository(db)
    low = repo.add("index", {"path": "a"}, priority=1)
    high = repo.add("index", {"path": "b"}, priority=5)
    assert repo.next_pending() == high
    assert repo.set_status(high.id, TaskStatus.RUNNING)
    assert repo.save_checkpoint(high.id, {"done": 3})
    got = repo.get(high.id)
    assert got is not None and got.status is TaskStatus.RUNNING and got.checkpoint == {"done": 3}
    assert got.payload == {"path": "b"}
    assert repo.next_pending() == low
    assert repo.reset_running() == 1
    assert repo.get(high.id).status is TaskStatus.PENDING  # type: ignore[union-attr]
    assert repo.get(high.id).checkpoint == {"done": 3}  # type: ignore[union-attr]
    assert repo.set_status(low.id, TaskStatus.FAILED, error="boom")
    assert [t.id for t in repo.list_by_status()] == [high.id]
    assert [t.id for t in repo.list_by_status(TaskStatus.FAILED)] == [low.id]
    assert repo.delete(low.id) and repo.get(low.id) is None
    assert repo.set_status("missing", TaskStatus.DONE) is False


def test_temporary_grants(db: Database) -> None:
    repo = TemporaryGrantRepository(db)
    live = repo.add(expires_at=NOW + timedelta(hours=1), scope="task:1", max_risk="medium")
    dead = repo.add(expires_at=NOW - timedelta(minutes=1), agent="coding", tool="git")
    assert [g.grant_id for g in repo.list_active(now=NOW)] == [live.grant_id]
    assert live.max_risk == "medium" and live.scope == "task:1" and live.revoked_at is None
    assert repo.revoke(live.grant_id, now=NOW) is True
    assert repo.revoke(live.grant_id) is False
    assert repo.list_active(now=NOW) == []
    assert repo.purge(now=NOW) == 2
    assert repo.get(dead.grant_id) is None


def test_proactive_notifications_add_get_list_recent(db: Database) -> None:
    repo = NotificationRepository(db)
    older = repo.add(
        id="older",
        priority="normal",
        kind="proactive",
        body="You have 3 new emails",
        created_at=NOW - timedelta(minutes=5),
    )
    newer = repo.add(
        id="newer",
        priority="security",
        kind="urgent",
        body="Kill switch armed",
        channel="speech+toast",
        spoken=True,
        announced=False,
        source="security_engine",
        created_at=NOW,
    )
    assert older.body == "You have 3 new emails" and older.title == ""
    assert newer.spoken is True and newer.source == "security_engine"
    assert repo.get("older") == older
    assert repo.get("missing") is None
    # newest first, matching HealthHistoryRepository.list_recent's convention
    assert [n.id for n in repo.list_recent()] == ["newer", "older"]
    assert [n.id for n in repo.list_recent(1)] == ["newer"]


def test_proactive_notifications_dismiss(db: Database) -> None:
    repo = NotificationRepository(db)
    repo.add(id="n1", priority="normal", kind="proactive", body="hint", created_at=NOW)
    assert repo.get("n1").dismissed_at is None  # type: ignore[union-attr]

    assert repo.dismiss("n1", dismissed_at=NOW) is True
    assert repo.get("n1").dismissed_at == NOW  # type: ignore[union-attr]
    assert repo.dismiss("n1") is False  # already dismissed -> no-op
    assert repo.dismiss("missing") is False

    assert [n.id for n in repo.list_recent(include_dismissed=False)] == []
    assert [n.id for n in repo.list_recent(include_dismissed=True)] == ["n1"]


def test_proactive_notifications_purge_expired(db: Database) -> None:
    repo = NotificationRepository(db)
    repo.add(
        id="expired_ttl",
        priority="normal",
        kind="proactive",
        body="stale hint",
        created_at=NOW - timedelta(days=1),
        expires_at=NOW - timedelta(minutes=1),
    )
    repo.add(
        id="live",
        priority="normal",
        kind="proactive",
        body="fresh hint",
        created_at=NOW,
    )
    repo.add(
        id="dismissed_old",
        priority="normal",
        kind="proactive",
        body="old, dismissed long ago",
        created_at=NOW - timedelta(days=40),
    )
    repo.dismiss("dismissed_old", dismissed_at=NOW - timedelta(days=35))
    repo.add(
        id="dismissed_recent",
        priority="normal",
        kind="proactive",
        body="dismissed just now",
        created_at=NOW,
    )
    repo.dismiss("dismissed_recent", dismissed_at=NOW)

    removed = repo.purge_expired(now=NOW, retention_days=30)

    assert removed == 2  # expired_ttl (own TTL) + dismissed_old (past the 30-day retention window)
    remaining = {n.id for n in repo.list_recent(100)}
    assert remaining == {"live", "dismissed_recent"}

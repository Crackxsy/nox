"""nox.data.repos: typed rows, retention/purge rules, ordering."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nox.data.db import Database
from nox.data.repos import (
    HealthHistoryRepository,
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

"""#28: `NotificationStore` persists via `NotificationRepository` (migration
`0010_notifications.sql`) when given a `db`, and keeps its original in-memory-ring-buffer
behaviour when constructed without one (`db=None`, e.g. every existing bare-`ProactiveService()`
unit test)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nox.data.db import Database
from nox.proactive.models import NotificationRecord
from nox.proactive.store import NotificationStore

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "nox.db")
    database.migrate()
    yield database
    database.close()


def _record(**overrides: object) -> NotificationRecord:
    defaults: dict[str, object] = dict(
        kind="proactive", priority="normal", text="hint", channel="toast", created_at=NOW
    )
    defaults.update(overrides)
    return NotificationRecord(**defaults)  # type: ignore[arg-type]


# ---- in-memory (db=None) behaviour is unchanged --------------------------------------------


def test_in_memory_store_round_trips_newest_first() -> None:
    store = NotificationStore(limit=200)
    first = _record(text="one", created_at=NOW - timedelta(minutes=1))
    second = _record(text="two", created_at=NOW)
    store.add(first)
    store.add(second)
    assert [r.text for r in store.list_recent()] == ["two", "one"]
    assert len(store) == 2


def test_in_memory_store_respects_limit_as_a_ring_buffer() -> None:
    store = NotificationStore(limit=2)
    for i in range(5):
        store.add(_record(text=str(i)))
    assert len(store) == 2
    assert [r.text for r in store.list_recent()] == ["4", "3"]


def test_in_memory_store_dismiss_round_trip() -> None:
    store = NotificationStore(limit=10)
    record = _record()
    store.add(record)
    assert store.dismiss(record.id) is True
    assert store.list_recent()[0].dismissed_at is not None
    assert store.dismiss(record.id) is False  # already dismissed
    assert store.dismiss("missing") is False


def test_in_memory_store_purge_expired_is_a_no_op() -> None:
    store = NotificationStore(limit=10)
    store.add(_record())
    assert store.purge_expired(now=NOW) == 0
    assert len(store) == 1


# ---- db-backed persistence (#28) -----------------------------------------------------------


def test_db_backed_store_survives_a_fresh_store_instance_same_db(db: Database) -> None:
    store = NotificationStore(limit=200, db=db)
    record = _record(text="warning", kind="urgent", priority="security")
    store.add(record)

    reopened = NotificationStore(limit=200, db=db)  # simulates a restart against the same DB
    recent = reopened.list_recent()

    assert len(recent) == 1
    assert recent[0].id == record.id
    assert recent[0].text == "warning"
    assert recent[0].kind == "urgent"
    assert recent[0].priority == "security"


def test_db_backed_store_dismiss_persists(db: Database) -> None:
    store = NotificationStore(limit=200, db=db)
    record = _record()
    store.add(record)

    assert store.dismiss(record.id) is True

    reopened = NotificationStore(limit=200, db=db)
    assert reopened.list_recent()[0].dismissed_at is not None


def test_db_backed_store_purge_expired_removes_old_dismissed_rows(db: Database) -> None:
    store = NotificationStore(limit=200, db=db)
    stale = _record(text="stale", created_at=NOW - timedelta(days=40))
    fresh = _record(text="fresh", created_at=NOW)
    store.add(stale)
    store.add(fresh)
    store.dismiss(stale.id, dismissed_at=NOW - timedelta(days=35))

    removed = store.purge_expired(now=NOW, retention_days=30)

    assert removed == 1
    assert [r.text for r in store.list_recent()] == ["fresh"]

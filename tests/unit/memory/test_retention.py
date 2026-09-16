"""nox.memory.retention.RetentionJob: nightly forgetting, audited (ST-07-01/07 subset)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nox.data.db import Database
from nox.data.repos import MemoryItemRepository
from nox.memory.retention import RetentionJob

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "nox.db")
    database.migrate()
    yield database
    database.close()


class RecordingAudit:
    def __init__(self) -> None:
        self.entries: list[dict[str, str]] = []

    def append(self, **kwargs: object) -> int:
        self.entries.append({k: str(v) for k, v in kwargs.items()})
        return len(self.entries)


async def test_run_purges_expired_memory_items_and_audits(db: Database) -> None:
    repo = MemoryItemRepository(db)
    expired = repo.add(
        type="conversation",
        text="old",
        importance=0.3,
        retain_until=NOW - timedelta(days=1),
    )
    kept = repo.add(type="conversation", text="new", importance=0.3, retain_until=None)
    audit = RecordingAudit()
    job = RetentionJob(repo, db, audit=audit)
    report = await job.run(now=NOW)
    assert report.memory_items_purged == 1
    assert repo.get(expired.id) is None
    assert repo.get(kept.id) is not None
    assert any(e["action"] == "retention.purge" for e in audit.entries)


async def test_run_purges_old_note_versions(db: Database) -> None:
    old_ts = (NOW - timedelta(days=60)).isoformat()
    recent_ts = (NOW - timedelta(days=1)).isoformat()
    db.execute(
        "INSERT INTO vault_note_versions (path, content, saved_at) VALUES ('a.md', 'old', ?)",
        (old_ts,),
    )
    db.execute(
        "INSERT INTO vault_note_versions (path, content, saved_at) VALUES ('b.md', 'recent', ?)",
        (recent_ts,),
    )
    repo = MemoryItemRepository(db)
    job = RetentionJob(repo, db, note_version_retention_days=30)
    report = await job.run(now=NOW)
    assert report.note_versions_purged == 1
    remaining = db.fetch_all("SELECT path FROM vault_note_versions")
    assert [dict(r)["path"] for r in remaining] == ["b.md"]


async def test_run_noop_when_nothing_expired(db: Database) -> None:
    repo = MemoryItemRepository(db)
    repo.add(type="conversation", text="fresh", importance=0.3, retain_until=None)
    job = RetentionJob(repo, db)
    report = await job.run(now=NOW)
    assert report.memory_items_purged == 0
    assert report.note_versions_purged == 0

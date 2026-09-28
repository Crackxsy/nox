"""The retention job purges every retention-bound table on schedule; health says how it went."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from nox.core.boot.retention import (
    RETENTION_FIRST_RUN_DELAY_S,
    RETENTION_INTERVAL_S,
    RetentionRule,
    RetentionService,
    retention_rules,
)
from nox.core.config import NoxConfig
from nox.core.events import HealthStatus
from nox.data.db import Database

T0 = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)


def iso(delta: timedelta) -> str:
    return (T0 + delta).isoformat()


DAY = timedelta(days=1)
HOUR = timedelta(hours=1)


class RecordingAudit:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def append(self, **entry: Any) -> int:
        self.entries.append(entry)
        return len(self.entries)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "nox.db")
    database.migrate()
    yield database
    database.close()


def seed(db: Database) -> None:
    """For every table: rows that must go (`old`/`expired`) and rows that must stay (`keep`)."""
    x = db.execute
    x(
        "INSERT INTO sessions (id, started_at, mode, privacy_mode) VALUES ('s', ?, 'm', 'p')",
        (iso(-DAY),),
    )
    turn = (
        "INSERT INTO turns (session_id, ts, role, text, retain_until) VALUES ('s', ?, 'user', ?, ?)"
    )
    x(turn, (iso(-8 * DAY), "old, written with no expiry", None))
    x(turn, (iso(-DAY), "expired", iso(-HOUR)))
    x(turn, (iso(-DAY), "keep", iso(6 * DAY)))
    viewer = (
        "INSERT INTO viewers (twitch_user_id, first_seen_at, last_seen_at, retain_until) "
        "VALUES (?, ?, ?, ?)"
    )
    x(viewer, ("gone", iso(-400 * DAY), iso(-400 * DAY), iso(-DAY)))
    x(viewer, ("inactive", iso(-800 * DAY), iso(-370 * DAY), None))  # never given an expiry
    x(viewer, ("kept", iso(-DAY), iso(-DAY), None))
    memory = (
        "INSERT INTO viewer_memory (viewer_id, what, created_at, retain_until) "
        "VALUES ('kept', ?, ?, ?)"
    )
    x(memory, ("expired", iso(-DAY), iso(-HOUR)))
    x(memory, ("keep", iso(-DAY), iso(DAY)))
    chat = "INSERT INTO chat_events (ts, text, retain_until) VALUES (?, ?, ?)"
    x(chat, (iso(-8 * DAY), "expired", iso(-DAY)))
    x(chat, (iso(-DAY), "keep", iso(6 * DAY)))
    health = "INSERT INTO health_history (ts, component, status) VALUES (?, 'db', 'available')"
    x(health, (iso(-400 * DAY),))
    x(health, (iso(-DAY),))
    note = (
        "INSERT INTO proactive_notifications (id, created_at, priority, kind, dismissed_at, "
        "expires_at) VALUES (?, ?, 'normal', 'hint', ?, ?)"
    )
    x(note, ("dismissed-long-ago", iso(-50 * DAY), iso(-40 * DAY), None))
    x(note, ("expired", iso(-2 * DAY), None, iso(-HOUR)))
    x(note, ("keep", iso(-DAY), iso(-DAY), None))
    item = (
        "INSERT INTO memory_items (type, text, created_at, retain_until) VALUES ('fact', ?, ?, ?)"
    )
    x(item, ("expired", iso(-10 * DAY), iso(-DAY)))
    x(item, ("keep", iso(-DAY), iso(DAY)))
    version = "INSERT INTO vault_note_versions (path, content, saved_at) VALUES ('n.md', ?, ?)"
    x(version, ("old", iso(-40 * DAY)))
    x(version, ("keep", iso(-DAY)))
    x("INSERT INTO rl_matches (started_at) VALUES (?)", (iso(-400 * DAY),))
    x("INSERT INTO rl_matches (started_at) VALUES (?)", (iso(-DAY),))
    event = "INSERT INTO rl_events (ts, kind, source, retain_until) VALUES (?, 'goal', 'hud', ?)"
    x(event, (iso(-200 * DAY), iso(-DAY)))
    x(event, (iso(-DAY), iso(100 * DAY)))
    frame = (
        "INSERT INTO rl_vision_frames (ts, entity, x, y, w, h, retain_until) "
        "VALUES (?, 'ball', 0, 0, 1, 1, ?)"
    )
    x(frame, (iso(-DAY), iso(-HOUR)))
    x(frame, (iso(-HOUR), iso(HOUR)))
    grant = "INSERT INTO temporary_grants (grant_id, expires_at, created_at) VALUES (?, ?, ?)"
    x(grant, ("expired", iso(-HOUR), iso(-DAY)))
    x(grant, ("keep", iso(HOUR), iso(-DAY)))
    pairing = (
        "INSERT INTO remote_pairings (id, code_hash, salt, created_at, expires_at) "
        "VALUES (?, 'h', 's', ?, ?)"
    )
    x(pairing, ("expired", iso(-HOUR), iso(-HOUR / 2)))
    x(pairing, ("keep", iso(-HOUR / 12), iso(HOUR)))
    for version_no in range(205):
        x(
            "INSERT INTO state_checkpoints (version, ts, json) VALUES (?, ?, '{}')",
            (version_no, iso(-HOUR)),
        )


def count(db: Database, table: str) -> int:
    row = db.fetch_one(f"SELECT COUNT(*) FROM {table}")  # noqa: S608 - test-owned table names
    assert row is not None
    return int(row[0])


def config_for(db: Database, **overrides: Any) -> NoxConfig:
    """Defaults, with every path inside the test's directory - never the real data folder."""
    return NoxConfig.model_validate({"paths": {"data_dir": str(db.path.parent)}, **overrides})


def seed_backups(db: Database) -> Path:
    backups = config_for(db).paths.backups_dir
    backups.mkdir(parents=True)
    for name, age in (("nox-1-before-0010_x.db", 40 * DAY), ("nox-2-before-0011_y.db", 2 * DAY)):
        (backups / name).write_bytes(b"backup")
        stamp = (T0 - age).timestamp()
        os.utime(backups / name, (stamp, stamp))
    return backups


def service(db: Database, audit: RecordingAudit | None = None, **kwargs: Any) -> RetentionService:
    return RetentionService(
        retention_rules(db, config_for(db), audit=audit), audit=audit, now=lambda: T0, **kwargs
    )


async def test_every_retention_bound_table_loses_its_expired_rows_and_keeps_the_rest(
    db: Database,
) -> None:
    seed(db)
    backups = seed_backups(db)
    audit = RecordingAudit()

    run = await service(db, audit).run_once()

    assert run.failed == {}
    assert run.removed == {
        "turns": 2,
        "chat_events": 1,
        "viewer_memory": 1,
        "viewers": 2,
        "health_history": 1,
        "proactive_notifications": 2,
        "memory_items, vault_note_versions": 2,
        "rl_events": 1,
        "rl_matches": 1,
        "rl_vision_frames": 1,
        "temporary_grants": 1,
        "remote_pairings": 1,
        "database backups": 1,
        "state_checkpoints": 5,
    }
    remaining = {
        "turns": 1,
        "chat_events": 1,
        "viewer_memory": 1,
        "viewers": 1,
        "health_history": 1,
        "proactive_notifications": 1,
        "memory_items": 1,
        "vault_note_versions": 1,
        "rl_events": 1,
        "rl_matches": 1,
        "rl_vision_frames": 1,
        "temporary_grants": 1,
        "remote_pairings": 1,
        "state_checkpoints": 200,
    }
    assert {table: count(db, table) for table in remaining} == remaining
    assert [p.name for p in backups.iterdir()] == ["nox-2-before-0011_y.db"]
    purge = [e for e in audit.entries if e["tool"] == "retention"]
    assert len(purge) == 1 and purge[0]["details"]["turns"] == "2"
    # memory items are audited one by one by the memory job, as before
    assert [e["target"] for e in audit.entries if e["tool"] == "memory"] == ["1", "1 rows"]


async def test_a_second_run_finds_nothing_left_to_remove(db: Database) -> None:
    seed(db)
    job = service(db)
    await job.run_once()

    again = await job.run_once()

    assert again.total == 0


async def test_health_reports_the_last_run(db: Database) -> None:
    job = service(db)
    status, reason = await job.health()
    assert status is HealthStatus.LIMITED and "not run yet" in reason

    await job.run_once()

    status, reason = await job.health()
    assert status is HealthStatus.AVAILABLE
    assert "last run 2026-09-28 12:00 UTC" in reason


async def test_one_failing_table_is_reported_and_does_not_stop_the_others(db: Database) -> None:
    seed(db)
    audit = RecordingAudit()

    def broken(_now: datetime) -> int:
        raise RuntimeError("disk I/O error")

    rules = [RetentionRule("broken_table", "x.y", broken), *retention_rules(db, config_for(db))]
    job = RetentionService(rules, audit=audit, now=lambda: T0)

    run = await job.run_once()

    assert run.failed == {"broken_table": "RuntimeError: disk I/O error"}
    assert run.removed["turns"] == 2
    status, reason = await job.health()
    assert status is HealthStatus.LIMITED
    assert "failed for broken_table" in reason and "no complete run yet" in reason
    assert audit.entries[-1]["result"] == "partial"


async def test_it_runs_after_the_boot_delay_and_then_on_its_interval(db: Database) -> None:
    waits: list[float] = []
    runs = 0
    parked = asyncio.Event()

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == 3:
            parked.set()
            await asyncio.Event().wait()  # park: the test stops the service here

    job = service(db, sleep=sleep)
    original = job.run_once

    async def counted() -> Any:
        nonlocal runs
        runs += 1
        return await original()

    job.run_once = counted  # type: ignore[method-assign]
    job.start()
    await asyncio.wait_for(parked.wait(), 5)
    await job.stop()

    assert waits == [RETENTION_FIRST_RUN_DELAY_S, RETENTION_INTERVAL_S, RETENTION_INTERVAL_S]
    assert runs == 2


async def test_transcripts_kept_forever_by_setting_are_not_purged_by_age(db: Database) -> None:
    seed(db)
    config = config_for(db, privacy={"retention": {"raw_transcripts_days": 0}})
    job = RetentionService(retention_rules(db, config), now=lambda: T0)

    run = await job.run_once()

    assert run.removed["turns"] == 1  # only the row with its own, passed expiry

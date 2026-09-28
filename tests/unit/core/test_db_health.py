"""The `db` health check: integrity checks on a schedule, never a full scan every 30 seconds."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any, cast

from nox.core.boot.health import (
    DB_INTEGRITY_CHECK_INTERVAL_S,
    DB_QUICK_CHECK_INTERVAL_S,
    DatabaseProbe,
)
from nox.core.boot.persistence import DatabaseRecovery, OpenedDatabase
from nox.core.events import HealthStatus
from tests.unit.fakes import MonotonicClock


class SpyDatabase:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.ok = True
        self.gate: threading.Event | None = None

    def integrity_check(self) -> bool:
        self.calls.append("integrity_check")
        return self._result()

    def quick_check(self) -> bool:
        self.calls.append("quick_check")
        return self._result()

    def _result(self) -> bool:
        if self.gate is not None:
            self.gate.wait(5)
        return self.ok


def probe_for(db: SpyDatabase, clock: MonotonicClock, **kwargs: Any) -> DatabaseProbe:
    return DatabaseProbe(lambda: cast("Any", db), clock=clock, **kwargs)


async def test_the_regular_rounds_do_not_scan_the_database() -> None:
    db, clock = SpyDatabase(), MonotonicClock()
    probe = probe_for(db, clock)

    for _ in range(100):  # 50 minutes of 30-second health rounds
        assert await probe.probe() == (HealthStatus.AVAILABLE, "ok")
        clock.advance(30)

    assert db.calls == []  # boot already ran the full check


async def test_a_quick_check_runs_hourly_and_a_full_check_daily() -> None:
    db, clock = SpyDatabase(), MonotonicClock()
    probe = probe_for(db, clock)

    clock.advance(DB_QUICK_CHECK_INTERVAL_S)
    await probe.probe()
    await probe.probe()
    assert db.calls == ["quick_check"]

    clock.advance(DB_INTEGRITY_CHECK_INTERVAL_S)
    await probe.probe()
    assert db.calls == ["quick_check", "integrity_check"]


async def test_a_failed_check_reports_corruption_until_the_next_one() -> None:
    db, clock = SpyDatabase(), MonotonicClock()
    probe = probe_for(db, clock)
    db.ok = False
    clock.advance(DB_QUICK_CHECK_INTERVAL_S)

    assert await probe.probe() == (HealthStatus.UNAVAILABLE, "corrupt")
    clock.advance(30)
    assert await probe.probe() == (HealthStatus.UNAVAILABLE, "corrupt")


async def test_a_slow_check_is_not_stacked_on_top_of_itself() -> None:
    db, clock = SpyDatabase(), MonotonicClock()
    db.gate = threading.Event()
    probe = probe_for(db, clock)
    clock.advance(DB_INTEGRITY_CHECK_INTERVAL_S)

    try:
        await asyncio.wait_for(probe.probe(), 0.05)  # HealthService's timeout gives up on it
    except TimeoutError:
        pass
    clock.advance(DB_INTEGRITY_CHECK_INTERVAL_S)
    status = await asyncio.wait_for(probe.probe(), 1.0)  # answers from the last result

    assert status == (HealthStatus.AVAILABLE, "ok")
    assert db.calls == ["integrity_check"]
    db.gate.set()


async def test_a_database_set_aside_at_boot_is_reported_loudly(tmp_path: Path) -> None:
    db, clock = SpyDatabase(), MonotonicClock()
    opened = OpenedDatabase(
        db=cast("Any", db),
        recovery=DatabaseRecovery(
            moved_to=tmp_path / "nox.db.corrupt-20260928120000", reason="cannot open"
        ),
    )
    probe = DatabaseProbe(lambda: cast("Any", db), lambda: opened, clock=clock)

    status, reason = await probe.probe()

    assert status is HealthStatus.LIMITED
    assert "nox.db.corrupt-20260928120000" in reason and str(tmp_path) not in reason


async def test_a_migration_without_backup_is_reported() -> None:
    db, clock = SpyDatabase(), MonotonicClock()
    opened = OpenedDatabase(db=cast("Any", db), backup_error="OSError: disk full")
    probe = DatabaseProbe(lambda: cast("Any", db), lambda: opened, clock=clock)

    status, reason = await probe.probe()

    assert status is HealthStatus.LIMITED and "without a backup" in reason

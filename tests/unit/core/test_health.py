"""nox.core.health: probes with timeouts, change detection, events, persistence, periodic loop."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from nox.core.bus import AsyncEventBus
from nox.core.events import E, Event, HealthStatus
from nox.core.health import Check, HealthService
from nox.data.db import Database
from nox.data.repos import HealthHistoryRepository
from tests.unit.fakes import FakeState


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[HealthHistoryRepository]:
    db = Database(tmp_path / "nox.db")
    db.migrate()
    yield HealthHistoryRepository(db)
    db.close()


async def ok() -> tuple[HealthStatus, str]:
    return HealthStatus.AVAILABLE, ""


async def limited() -> tuple[HealthStatus, str]:
    return HealthStatus.LIMITED, "no gpu"


async def broken() -> tuple[HealthStatus, str]:
    raise ConnectionError("refused")


async def slow() -> tuple[HealthStatus, str]:
    await asyncio.sleep(5)
    return HealthStatus.AVAILABLE, ""


async def test_run_once_reports_honestly(repo: HealthHistoryRepository) -> None:
    bus = AsyncEventBus()
    changed: list[Event] = []
    reports: list[Event] = []
    bus.subscribe(E.SYSTEM_HEALTH_CHANGED, changed.append)
    bus.subscribe(E.HEALTH_REPORT, reports.append)
    state = FakeState()
    svc = HealthService(
        bus,
        repo,
        [
            Check("ollama", ok),
            Check("gpu", limited),
            Check("vault", broken),
            Check("stt", slow, 0.05),
        ],
        state_manager=state,
    )
    report = await svc.run_once()
    comps = report.components
    assert comps["ollama"].status is HealthStatus.AVAILABLE
    assert comps["gpu"].status is HealthStatus.LIMITED and comps["gpu"].reason == "no gpu"
    assert comps["vault"].status is HealthStatus.UNAVAILABLE and "refused" in comps["vault"].reason
    assert comps["stt"].status is HealthStatus.UNAVAILABLE and "timeout" in comps["stt"].reason
    assert len(changed) == 4 and len(reports) == 1
    assert reports[0].payload["components"]["stt"]["status"] == "unavailable"
    assert state.get("system.health") == {
        "ollama": "available",
        "gpu": "limited",
        "vault": "unavailable",
        "stt": "unavailable",
    }
    assert set(repo.latest_per_component()) == {"ollama", "gpu", "vault", "stt"}


async def test_only_changes_emit_and_persist(repo: HealthHistoryRepository) -> None:
    bus = AsyncEventBus()
    changed: list[Event] = []
    bus.subscribe(E.SYSTEM_HEALTH_CHANGED, changed.append)
    status = {"value": HealthStatus.AVAILABLE}

    async def flapping() -> tuple[HealthStatus, str]:
        return status["value"], ""

    svc = HealthService(bus, repo, [Check("api", flapping)])
    await svc.run_once()
    await svc.run_once()
    assert len(changed) == 1
    status["value"] = HealthStatus.UNAVAILABLE
    await svc.run_once()
    assert len(changed) == 2 and changed[1].payload["status"] == "unavailable"
    assert len(repo.list_recent(component="api")) == 2
    assert svc.current()["api"].status is HealthStatus.UNAVAILABLE


async def test_periodic_loop_and_check_management() -> None:
    bus = AsyncEventBus()
    svc = HealthService(bus, None, [Check("a", ok)], interval_s=0.02)
    with pytest.raises(ValueError):
        svc.add_check(Check("a", ok))
    svc.start()
    await asyncio.sleep(0.1)
    await svc.stop()
    assert svc.runs >= 2
    svc.remove_check("a")
    report = await svc.run_once()
    assert report.components == {}

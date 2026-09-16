"""`nox.pm.install.install` must return without scanning the vault.

The scan is ~4 s of synchronous file reads on the product owner's vault (184 notes); running it
inline inside `NoxCore.start()` froze the event loop, the core stopped heartbeating, and the
supervisor restarted a perfectly healthy boot (2026-09-15). It now runs in a background task and
the `pm` health check says `limited` until it is done.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nox.core.events import HealthStatus
from nox.core.health import HealthService
from nox.data.db import Database
from nox.pm.install import PmRuntime, install
from nox.tools.registry import ToolRegistry
from tests.unit.fakes import FakeBus


def make_core(db: Database, vault_dir: Path) -> Any:
    pm_cfg = SimpleNamespace(
        epics_dir="08 - Epics",
        stories_dir="09 - Stories",
        projects_note="Projects.md",
        focus_max_items=5,
        watch_debounce_s=0.05,
    )
    bus = FakeBus()
    return SimpleNamespace(
        config=SimpleNamespace(paths=SimpleNamespace(vault_dir=vault_dir), pm=pm_cfg),
        db=db,
        bus=bus,
        state=None,
        tool_registry=ToolRegistry(),
        health=HealthService(bus),
    )


@pytest.fixture
async def installed(db: Database, vault_dir: Path) -> AsyncIterator[tuple[Any, PmRuntime]]:
    core = make_core(db, vault_dir)
    runtime = install(core)
    try:
        yield core, runtime
    finally:
        runtime.stop()


async def test_install_returns_before_the_vault_is_indexed(
    installed: tuple[Any, PmRuntime],
) -> None:
    _core, runtime = installed
    assert not runtime.indexed.is_set()
    assert runtime.index.list_items() == []  # nothing read yet, and nothing faked either
    assert await runtime.ready(timeout=10.0)
    assert runtime.item_count == 4
    assert {item.id for item in runtime.index.list_items()} >= {"EPIC-13", "ST-13-01", "ST-13-02"}


async def test_install_does_not_block_the_event_loop(installed: tuple[Any, PmRuntime]) -> None:
    """The loop keeps ticking (heartbeats, IPC) while the vault is being read."""
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        assert await installed[1].ready(timeout=10.0)
    finally:
        task.cancel()
    assert ticks > 0


async def test_health_reports_limited_until_indexed(installed: tuple[Any, PmRuntime]) -> None:
    core, runtime = installed
    report = await core.health.run_once()
    assert report.components["pm"].status is HealthStatus.LIMITED
    assert await runtime.ready(timeout=10.0)
    report = await core.health.run_once()
    assert report.components["pm"].status is HealthStatus.AVAILABLE

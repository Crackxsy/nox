"""`nox.day.install`: the extension wires itself onto a core; a due reminder reaches proactive."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nox.core.config import NoxConfig
from nox.core.events import HealthStatus
from nox.data.db import Database
from nox.day.install import CALENDAR_SECRETS, install
from nox.ipc.dispatch import RequestRegistry
from nox.tools.registry import ToolRegistry


class _Secrets:
    def get(self, _name: str) -> str | None:
        return None


class _Health:
    def __init__(self) -> None:
        self.checks: dict[str, Any] = {}

    def add_check(self, check: Any) -> None:
        if check.name in self.checks:
            raise ValueError(check.name)
        self.checks[check.name] = check


class _Proactive:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def notify(self, kind: str, priority: str, text: str, **_kw: Any) -> None:
        self.calls.append((kind, priority, text))


@pytest.fixture
def core(tmp_path: Path) -> Iterator[Any]:
    db = Database(tmp_path / "nox.db")
    db.migrate()
    proactive = _Proactive()
    yield SimpleNamespace(
        config=NoxConfig(),
        db=db,
        security=SimpleNamespace(secrets=_Secrets(), egress=SimpleNamespace(client=None)),
        tool_registry=ToolRegistry(),
        registry=RequestRegistry(),
        extensions={"proactive": SimpleNamespace(service=proactive)},
        health=_Health(),
        proactive=proactive,
    )
    db.close()


async def test_install_registers_tools_requests_and_a_health_check(core: Any) -> None:
    runtime = install(core)
    assert runtime is not None
    try:
        assert {"calendar.today", "reminder.set"} <= set(core.tool_registry.names())
        assert {"day.today", "day.reminder.cancel"} <= set(core.registry.names())
        status, detail = await core.health.checks["day"].probe()
        assert status is HealthStatus.LIMITED and "no calendar" in detail
        assert len(CALENDAR_SECRETS) == 2
    finally:
        await runtime.stop()


async def test_a_due_reminder_is_spoken_through_proactive(core: Any) -> None:
    core.config = NoxConfig.model_validate({"day": {"reminder_poll_s": 5.0}})
    runtime = install(core)
    assert runtime is not None
    try:
        runtime.reminders.add(
            text="Zahnarzt anrufen", due_at=datetime.now(UTC) - timedelta(seconds=5)
        )
        fired = await runtime.scheduler.tick()
        assert len(fired) == 1
        assert core.proactive.calls == [("urgent", "reminder", "Erinnerung: Zahnarzt anrufen")]
    finally:
        await runtime.stop()


async def test_the_extension_can_be_switched_off(core: Any) -> None:
    core.config = NoxConfig.model_validate({"day": {"enabled": False}})
    assert install(core) is None
    await asyncio.sleep(0)

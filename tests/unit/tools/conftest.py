"""Shared fixtures: registry, a real permission engine (in-memory profile), fake audit/kill
switch."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from nox.core.events import E, Event, EventBus
from nox.core.state import PrivacyMode
from nox.security.audit import SqliteAuditLog
from nox.security.model import Profile, ProfileRule
from nox.security.permissions import (
    DefaultPermissionEngine,
    InMemoryGrantStore,
    PrivacySnapshot,
)
from nox.security.profiles import InMemoryProfileProvider
from nox.tools.registry import ToolRegistry
from tests.unit.fakes import FakeBus, MutableClock

START = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


class FakePrivacy:
    """Mutable `PrivacyStateProvider`: tests flip `.mode`/`.safe_mode` directly."""

    def __init__(self) -> None:
        self.mode = PrivacyMode.BALANCED
        self.zone_active = False
        self.safe_mode = False
        self.panic = False

    def snapshot(self) -> PrivacySnapshot:
        return PrivacySnapshot(
            mode=self.mode,
            zone_active=self.zone_active,
            safe_mode=self.safe_mode,
            panic=self.panic,
        )


class FakeKillSwitch:
    """Implements `nox.security.model.KillSwitch`; publishes `security.kill_switch` on trigger so
    `ToolExecutor` cancellation can be exercised without the real `KillSwitchService`."""

    def __init__(self, bus: EventBus | None = None) -> None:
        self._engaged = False
        self._bus = bus

    def is_engaged(self) -> bool:
        return self._engaged

    async def trigger(self, *, by: str, reason: str = "") -> None:
        self._engaged = True
        if self._bus is not None:
            await self._bus.publish(
                Event(name=E.SECURITY_KILL_SWITCH, payload={"by": by, "reason": reason})
            )

    def resume(self) -> None:
        self._engaged = False


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock(START)


@pytest.fixture
def bus() -> FakeBus:
    return FakeBus()


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def audit(conn: sqlite3.Connection, bus: FakeBus, clock: MutableClock) -> SqliteAuditLog:
    return SqliteAuditLog(conn, bus=bus, clock=clock)


@pytest.fixture
def privacy() -> FakePrivacy:
    return FakePrivacy()


@pytest.fixture
def killswitch(bus: FakeBus) -> FakeKillSwitch:
    return FakeKillSwitch(bus)


@pytest.fixture
def test_profile() -> Profile:
    return Profile(
        id="test",
        description="permissive test profile",
        rules=[
            ProfileRule(id="test.deny_secret", tool="secret", decision="deny"),
        ],
        cloud_allowed=True,
        memory_writes_allowed=True,
    )


@pytest.fixture
def profiles(test_profile: Profile) -> InMemoryProfileProvider:
    return InMemoryProfileProvider([test_profile])


@pytest.fixture
def engine(
    profiles: InMemoryProfileProvider,
    privacy: FakePrivacy,
    audit: SqliteAuditLog,
    bus: FakeBus,
    clock: MutableClock,
) -> DefaultPermissionEngine:
    return DefaultPermissionEngine(
        profiles=profiles,
        privacy=privacy,
        grants=InMemoryGrantStore(),
        audit=audit,
        bus=bus,
        clock=clock,
        initial_profile="test",
        confirm_timeout_s=0.2,
    )


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry()

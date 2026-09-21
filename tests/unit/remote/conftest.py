"""Fixtures for the remote (Mobile Companion) unit tests: a migrated on-disk database in `tmp_path`
(never a real data dir, per ENGINEERING.md), the repository/pairing/policy objects on top of it,
and small fakes for the kill switch, privacy service and outbound channel.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from nox.core.state import PrivacyMode
from nox.data.db import Database
from nox.remote.pairing import PairingService
from nox.remote.policy import RemoteCommandPolicy, RemoteRateLimiter
from nox.remote.repo import RemoteRepository
from tests.unit.fakes import Clock

START = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


class FakeAudit:
    """`nox.security.model.AuditLog` shape; keeps entries so tests can assert on them."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def append(self, **kwargs: Any) -> int:
        self.entries.append(kwargs)
        return len(self.entries)

    def verify_chain(self) -> bool:
        return True

    def actions(self) -> list[str]:
        return [str(e["action"]) for e in self.entries]


class FakeKillSwitch:
    """The slice `RemoteService` uses, plus the resume rule it must never be able to reach."""

    def __init__(self) -> None:
        self.engaged = False
        self.origins: list[str] = []
        self.security_path = False
        self.resumes: list[str] = []

    async def engage(self, origin: str, reason: str = "") -> dict[str, Any]:
        self.engaged = True
        self.origins.append(origin)
        # Mirrors `KillSwitchService`: only tamper/audit/panic/supervisor-tamper are security path.
        self.security_path = origin in {"tamper", "audit", "panic", "supervisor-tamper"}
        return {"engaged": True, "origin": origin, "security_path": self.security_path}

    async def resume(self, *, pin_ok: bool, by: str = "user") -> bool:
        self.resumes.append(by)
        if self.security_path and not pin_ok:
            return False
        self.engaged = False
        return True

    def is_engaged(self) -> bool:
        return self.engaged


class FakePrivacy:
    """`PrivacyService.set_mode` with the real asymmetry: to FULL needs `confirmed=True`."""

    def __init__(self, mode: PrivacyMode = PrivacyMode.BALANCED) -> None:
        self.mode = mode
        self.calls: list[tuple[str, str, bool]] = []
        self.active_zone: str | None = None

    async def set_mode(
        self, mode: PrivacyMode, *, by: str = "user", confirmed: bool = False
    ) -> Any:
        self.calls.append((mode.value, by, confirmed))
        applied = not (
            mode is PrivacyMode.FULL and self.mode is not PrivacyMode.FULL and not confirmed
        )
        if applied:
            self.mode = mode
        return type(
            "Change",
            (),
            {"current": self.mode, "applied": applied, "requires_confirmation": not applied},
        )()


class FakeChannel:
    """Captures every outbound message the core hands to `telegram.send`."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = False

    async def __call__(self, chat_id: str, text: str) -> None:
        if self.fail:
            raise RuntimeError("send failed")
        self.sent.append((chat_id, text))

    @property
    def texts(self) -> list[str]:
        return [t for _, t in self.sent]


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "nox.db")
    database.migrate()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def clock() -> Clock:
    return Clock(START)


@pytest.fixture
def repo(db: Database) -> RemoteRepository:
    return RemoteRepository(db)


@pytest.fixture
def audit() -> FakeAudit:
    return FakeAudit()


@pytest.fixture
def pairing(repo: RemoteRepository, audit: FakeAudit, clock: Clock, bus) -> PairingService:
    return PairingService(repo, bus=bus, audit=audit, ttl_s=300.0, max_devices=2, clock=clock)


@pytest.fixture
def bus():
    from tests.unit.fakes import FakeBus

    return FakeBus()


@pytest.fixture
def policy() -> RemoteCommandPolicy:
    return RemoteCommandPolicy(rate_limiter=RemoteRateLimiter(per_minute=600, burst=50))


@pytest.fixture
def ticker() -> Callable[[], float]:
    """A monotonic clock for the rate limiter, wound by hand."""
    state = {"t": 0.0}

    def _clock() -> float:
        return state["t"]

    _clock.advance = lambda seconds: state.__setitem__("t", state["t"] + seconds)  # type: ignore[attr-defined]
    return _clock

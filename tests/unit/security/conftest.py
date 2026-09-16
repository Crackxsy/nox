"""Shared fixtures: fake bus, in-memory SQLite audit, privacy service, real repo profiles, clock."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nox.core.state import PrivacyMode
from nox.security.audit import SqliteAuditLog
from nox.security.model import PermissionRequest, Risk
from nox.security.permissions import DefaultPermissionEngine, InMemoryGrantStore
from nox.security.privacy import PrivacyService
from nox.security.profiles import YamlProfileProvider
from tests.unit.fakes import FakeBus

REPO = Path(__file__).resolve().parents[3]
PROFILES_DIR = REPO / "config" / "profiles"
DEFAULTS_YAML = REPO / "config" / "defaults.yaml"


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def req(tool: str, action: str = "", risk: Risk = Risk.LOW, **kw: object) -> PermissionRequest:
    base: dict[str, object] = {
        "agent": "nox.chat",
        "tool": tool,
        "action": action,
        "mode": "companion",
        "risk": risk,
    }
    base.update(kw)
    return PermissionRequest.model_validate(base)


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def bus() -> FakeBus:
    return FakeBus()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def audit(conn: sqlite3.Connection, bus: FakeBus, clock: MutableClock) -> SqliteAuditLog:
    return SqliteAuditLog(conn, bus=bus, clock=clock)


@pytest.fixture
def safe_mode_flag() -> dict[str, bool]:
    return {"on": False}


@pytest.fixture
def privacy(
    bus: FakeBus, audit: SqliteAuditLog, clock: MutableClock, safe_mode_flag: dict[str, bool]
) -> PrivacyService:
    return PrivacyService(
        mode=PrivacyMode.BALANCED,
        capture={"microphone": True, "camera": False, "screen": True},
        zones=[
            "banking",
            "password_manager",
            "email",
            "private_chats",
            "personal_documents",
            "discord",
        ],
        bus=bus,
        audit=audit,
        clock=clock,
        safe_mode=lambda: safe_mode_flag["on"],
    )


@pytest.fixture
def profiles() -> YamlProfileProvider:
    return YamlProfileProvider(PROFILES_DIR)


@pytest.fixture
def grants() -> InMemoryGrantStore:
    return InMemoryGrantStore()


@pytest.fixture
def engine(
    profiles: YamlProfileProvider,
    privacy: PrivacyService,
    grants: InMemoryGrantStore,
    audit: SqliteAuditLog,
    bus: FakeBus,
    clock: MutableClock,
) -> DefaultPermissionEngine:
    return DefaultPermissionEngine(
        profiles=profiles,
        privacy=privacy,
        grants=grants,
        audit=audit,
        bus=bus,
        clock=clock,
        confirm_timeout_s=0.05,
        session_id="s1",
    )

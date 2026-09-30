"""Shared fixtures: fake bus, in-memory SQLite audit, privacy service, real repo profiles, clock."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from nox.core.config import NoxConfig
from nox.core.state import PrivacyMode
from nox.security.audit import SqliteAuditLog
from nox.security.model import PermissionRequest, Risk
from nox.security.permissions import DefaultPermissionEngine, InMemoryGrantStore
from nox.security.privacy import PrivacyService
from nox.security.profiles import YamlProfileProvider
from nox.security.service import SecurityContext
from tests.unit.fakes import FakeBus, MutableClock

REPO = Path(__file__).resolve().parents[3]
PROFILES_DIR = REPO / "config" / "profiles"
DEFAULTS_YAML = REPO / "config" / "defaults.yaml"
START = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


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
    return MutableClock(START)


@pytest.fixture
def bus() -> FakeBus:
    return FakeBus()


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    """A connection with the same threading settings the real database uses.

    `nox.data.db` opens its connection with `check_same_thread=False`, because the audit log is
    written from a background thread and every repository call goes through a worker thread. A
    test connection without that flag is stricter than production and fails on writes that are
    perfectly safe there - the audit log serialises its own access with a lock.
    """
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    yield connection
    connection.close()


@pytest.fixture
def build_security(conn: sqlite3.Connection) -> Iterator[Callable[..., SecurityContext]]:
    """Build a `SecurityContext` on the shared connection, and stop it before that connection goes.

    A context owns a `QueuedAuditLog`, whose writer runs on its own thread and shares this
    connection - guarded by the audit log's lock, not the database's. Closing the connection while
    that thread is inside a statement is an access violation rather than an exception: `sqlite3`
    does not raise there, the process faults. The suite died exactly that way once, in
    `audit.py:_last`, from the writer thread.

    Depending on `conn` is what fixes the order: pytest tears this down first, so every writer is
    stopped before the connection is closed. Building a context without this fixture puts the
    hazard back.
    """
    made: list[SecurityContext] = []

    def build(config: NoxConfig, **kwargs: Any) -> SecurityContext:
        context = SecurityContext.build(config, conn=conn, **kwargs)
        made.append(context)
        return context

    yield build
    for context in made:
        context.close()


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

"""Shared fixtures for nox.stream tests: migrated in-memory-file db, viewer/ledger repos, audit,
bus, mutable clock."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nox.core.config import FunkenConfig, RelevanceConfig, StreamChatConfig
from nox.data.db import Database
from nox.data.stream_repos import (
    ChatEventRepository,
    FunkenLedgerRepository,
    StreamSessionRepository,
    ViewerRepository,
)
from nox.security.audit import SqliteAuditLog
from tests.unit.fakes import FakeBus, MutableClock

NOW = datetime(2026, 9, 13, 20, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock(NOW)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "nox.db")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def viewers(db: Database) -> ViewerRepository:
    return ViewerRepository(db)


@pytest.fixture
def ledger(db: Database) -> FunkenLedgerRepository:
    return FunkenLedgerRepository(db)


@pytest.fixture
def stream_sessions_repo(db: Database) -> StreamSessionRepository:
    return StreamSessionRepository(db)


@pytest.fixture
def chat_events(db: Database) -> ChatEventRepository:
    return ChatEventRepository(db)


@pytest.fixture
def funken_config() -> FunkenConfig:
    return FunkenConfig()


@pytest.fixture
def chat_config() -> StreamChatConfig:
    return StreamChatConfig()


@pytest.fixture
def relevance_config() -> RelevanceConfig:
    return RelevanceConfig()


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

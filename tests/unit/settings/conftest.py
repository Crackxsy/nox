"""Fixtures for the Settings area: an in-memory keyring, a capturing audit log, and a user.yaml
under `tmp_path` - no test in here ever touches a real keyring, a real data dir or the network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from nox.core.config import NoxConfig, load_config
from nox.security.secrets import InMemorySecretStore
from tests.unit.fakes import FakeBus

DEFAULTS_PATH = Path(__file__).resolve().parents[3] / "config" / "defaults.yaml"
START = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


class FakeAudit:
    """`nox.security.model.AuditLog` shape; keeps entries so tests can assert on them."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def append(self, **kwargs: Any) -> int:
        self.entries.append(kwargs)
        return len(self.entries)

    def verify_chain(self) -> bool:
        return True

    def blob(self) -> str:
        """Everything that was ever audited, as one string - for "no value leaked" assertions."""
        return repr(self.entries)


class Clock:
    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def defaults_path() -> Path:
    return DEFAULTS_PATH


@pytest.fixture
def config() -> NoxConfig:
    return load_config(DEFAULTS_PATH)


@pytest.fixture
def user_config(tmp_path: Path) -> Path:
    return tmp_path / "Nox" / "user.yaml"


@pytest.fixture
def audit() -> FakeAudit:
    return FakeAudit()


@pytest.fixture
def bus() -> FakeBus:
    return FakeBus()


@pytest.fixture
def secrets() -> InMemorySecretStore:
    return InMemorySecretStore()


@pytest.fixture
def clock() -> Clock:
    return Clock()

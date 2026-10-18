"""Fixtures for the network-access tests: a migrated database, the device registry and a fresh
certificate authority per test (in `tmp_path`, never the real Nox folder)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from nox.data.db import Database
from nox.lan.devices import LanDevices
from nox.lan.tls import TlsMaterial, ensure_tls
from nox.remote.repo import RemoteRepository
from tests.unit.ipc.conftest import SimpleBus

#: What the tests' certificate is issued for: loopback, so real TLS connections can be made.
NAMES = ("localhost", "noxpi", "noxpi.local")
ADDRESSES = ("127.0.0.1", "192.168.1.20")


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "nox.db")
    database.migrate()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def repo(db: Database) -> RemoteRepository:
    return RemoteRepository(db)


@pytest.fixture
def bus() -> SimpleBus:
    return SimpleBus()


@pytest.fixture
def devices(repo: RemoteRepository, bus: SimpleBus) -> LanDevices:
    return LanDevices(repo, bus=bus, ttl_s=300.0, max_devices=3)  # type: ignore[arg-type]


@pytest.fixture
def tls(tmp_path: Path) -> TlsMaterial:
    return ensure_tls(tmp_path / "tls", names=NAMES, addresses=ADDRESSES)

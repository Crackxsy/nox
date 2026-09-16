"""nox.memory.items.MemoryService: the write-refusal boundary (ST-07-01 AC4)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from nox.data.db import Database
from nox.data.repos import MemoryItemRepository
from nox.memory.items import MemoryService, MemoryWriteRefusedError


class Gate:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed

    def allows_memory_write(self) -> bool:
        return self.allowed


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "nox.db")
    database.migrate()
    yield database
    database.close()


async def test_create_refused_when_privacy_forbids(db: Database) -> None:
    repo = MemoryItemRepository(db)
    svc = MemoryService(repo, Gate(allowed=False))
    with pytest.raises(MemoryWriteRefusedError):
        await svc.create("some fact", source="test")
    assert db.fetch_all("SELECT * FROM memory_items") == []


async def test_create_succeeds_when_allowed(db: Database) -> None:
    repo = MemoryItemRepository(db)
    svc = MemoryService(repo, Gate(allowed=True))
    row = await svc.create("merk dir: Lieblingsfarbe ist blau", source="test")
    assert row.importance == 1.0  # explicit command
    stored = repo.get(row.id)
    assert stored is not None and stored.text.startswith("merk dir")


async def test_delete_returns_false_for_unknown_id(db: Database) -> None:
    repo = MemoryItemRepository(db)
    svc = MemoryService(repo, Gate(allowed=True))
    assert await svc.delete(999) is False


async def test_delete_removes_row(db: Database) -> None:
    repo = MemoryItemRepository(db)
    svc = MemoryService(repo, Gate(allowed=True))
    row = await svc.create("a fact to delete", source="test")
    assert await svc.delete(row.id) is True
    assert repo.get(row.id) is None

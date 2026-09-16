"""nox.memory.tools: input validation, path-traversal protection on `vault.read`, refusal paths
returned as structured results rather than exceptions."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from nox.data.db import Database
from nox.data.repos import MemoryItemRepository
from nox.memory.items import MemoryService
from nox.memory.tools import make_memory_write_tool, make_vault_read_tool


class Gate:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed

    def allows_memory_write(self) -> bool:
        return self.allowed


class NoZones:
    def path_zone(self, path: str) -> str | None:
        return None


class AlwaysZoned:
    def path_zone(self, path: str) -> str | None:
        return "zone"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "nox.db")
    database.migrate()
    yield database
    database.close()


async def test_vault_read_returns_file_contents(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("hello", encoding="utf-8")
    tool = make_vault_read_tool(vault, NoZones())
    result = await tool.handler({"path": "note.md"})
    assert result == {"ok": True, "path": "note.md", "text": "hello"}


async def test_vault_read_blocks_path_traversal(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (tmp_path / "outside.md").write_text("secret", encoding="utf-8")
    tool = make_vault_read_tool(vault, NoZones())
    result = await tool.handler({"path": "../outside.md"})
    assert result["ok"] is False
    assert "escapes" in result["reason"]


async def test_vault_read_refuses_zoned_path(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "bank.md").write_text("x", encoding="utf-8")
    tool = make_vault_read_tool(vault, AlwaysZoned())
    result = await tool.handler({"path": "bank.md"})
    assert result == {"ok": False, "reason": "privacy zone"}


async def test_vault_read_not_found(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    tool = make_vault_read_tool(vault, NoZones())
    result = await tool.handler({"path": "missing.md"})
    assert result == {"ok": False, "reason": "not found"}


async def test_memory_write_tool_returns_structured_refusal(db: Database) -> None:
    repo = MemoryItemRepository(db)
    memory = MemoryService(repo, Gate(allowed=False))
    tool = make_memory_write_tool(memory)
    result = await tool.handler({"text": "a fact"})
    assert result["ok"] is False


async def test_memory_write_tool_succeeds(db: Database) -> None:
    repo = MemoryItemRepository(db)
    memory = MemoryService(repo, Gate(allowed=True))
    tool = make_memory_write_tool(memory)
    result = await tool.handler({"text": "merk dir das", "source": "test"})
    assert result["ok"] is True
    assert result["importance"] == 1.0

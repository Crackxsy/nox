"""`VaultIndexer.full_scan` must keep the event loop responsive: a scan over an already-indexed
vault has no I/O await, and without explicit yields it starved the supervisor heartbeat and the
worker/shell IPC handshakes (2026-09-15)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from nox.data.db import Database
from nox.memory.vault_index import VaultIndexer


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "nox.db")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    v.mkdir()
    for i in range(40):
        (v / f"note-{i:02d}.md").write_text(
            f"---\ntitle: Note {i}\n---\n\n# Note {i}\n\n" + ("lorem ipsum " * 200) + "\n",
            encoding="utf-8",
        )
    return v


async def _ticks_during(coro: object) -> int:
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        await coro  # type: ignore[misc]
    finally:
        task.cancel()
    return ticks


async def test_first_scan_yields_between_chunks(db: Database, vault: Path) -> None:
    idx = VaultIndexer(db, vault, scan_yield_s=0.0)
    ticks = await _ticks_during(idx.full_scan())
    assert ticks >= 40  # at least once per note, in practice once per chunk


async def test_rescan_of_unchanged_vault_still_yields(db: Database, vault: Path) -> None:
    idx = VaultIndexer(db, vault, scan_yield_s=0.0)
    await idx.full_scan()
    stats = None

    async def rescan() -> None:
        nonlocal stats
        stats = await idx.full_scan()

    ticks = await _ticks_during(rescan())
    assert stats is not None and stats.unchanged == 40
    assert ticks >= 30  # once per note minus scheduling slack

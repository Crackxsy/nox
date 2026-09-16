"""nox.memory.vault_watcher: a burst of rapid saves collapses into exactly one re-index (ST-07-03
AC1 / SP-13 debounce measurement). Uses a real filesystem + real `watchdog` observer thread since
the debounce logic is exactly what is under test; kept fast with a short debounce window."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nox.data.db import Database
from nox.memory.vault_index import VaultIndexer
from nox.memory.vault_watcher import VaultWatcher


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "nox.db")
    database.migrate()
    return database


async def test_burst_of_saves_reindexes_exactly_once(db: Database, tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    idx = VaultIndexer(db, vault)
    watcher = VaultWatcher(idx, vault, debounce_s=0.2)
    watcher.start()
    try:
        note = vault / "burst.md"
        for i in range(6):
            note.write_text(f"# Burst\n\nversion {i}\n", encoding="utf-8")
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.6)  # debounce window + margin for the reindex task to run
        assert watcher.reindex_count == 1
        row = db.fetch_one("SELECT * FROM vault_index WHERE path = ?", ("burst.md",))
        assert row is not None
    finally:
        watcher.stop()
        db.close()

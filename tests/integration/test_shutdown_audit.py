"""Shutdown must not pull the SQLite connection out from under the audit writer.

This is a regression test for a crash, not an exception. The queued audit writer runs on its own
thread and shares the core's SQLite connection, guarded by its own lock rather than the database's.
Shutdown drains the writer with a five-second budget and, when that budget ran out, used to close
the connection anyway - and `sqlite3` does not raise when a statement's connection disappears
underneath it, it faults. The suite died with `Windows fatal exception: access violation` in
`audit.py:_last`, from the writer thread, once enough tool calls were being audited.

Reproducing a real timeout would mean a race. What is asserted instead is the decision that
keeps the race harmless: a shutdown that could not drain the writer leaves the connection open
and says so.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from nox.app import DEFAULTS_PATH, NoxCore
from nox.core.config import load_config
from tests._ports import free_port_base


def _config(tmp_path: Path) -> Any:
    base = free_port_base()
    (tmp_path / "vault").mkdir()
    overrides = {
        "paths": {
            "data_dir": str(tmp_path / "data"),
            "vault_dir": str(tmp_path / "vault"),
            "index_dir": str(tmp_path / "index"),
            "database_dir": str(tmp_path / "db"),
            "cache_dir": str(tmp_path / "cache"),
            "backups_dir": str(tmp_path / "backups"),
            "runtime_dir": str(tmp_path / "runtime"),
            "logs_dir": str(tmp_path / "logs"),
        },
        "ipc": {"port": base, "http_port": base + 1},
        "health": {"check_interval_s": 3600},
        "logging": {"level": "WARNING"},
        "plugins": {"enabled": []},
    }
    return load_config(DEFAULTS_PATH, None, None, overrides)


async def _started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> NoxCore:
    user_config = tmp_path / "user.yaml"
    user_config.write_text(yaml.safe_dump({}), encoding="utf-8")
    monkeypatch.setenv("NOX_USER_CONFIG", str(user_config))
    monkeypatch.setenv("NOX_CONFIG_DEFAULTS", str(DEFAULTS_PATH))
    core = NoxCore(_config(tmp_path), voice=False)
    await asyncio.wait_for(core.start(), timeout=60)
    return core


def _usable(connection: sqlite3.Connection) -> bool:
    try:
        connection.execute("SELECT 1").fetchone()
    except sqlite3.ProgrammingError:
        return False
    return True


async def test_a_clean_shutdown_closes_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = await _started(tmp_path, monkeypatch)
    connection = core.db.connection

    await asyncio.wait_for(core.stop(), timeout=30)

    assert not _usable(connection), "the normal path still has to release the connection"


async def test_a_writer_that_did_not_finish_keeps_its_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leaked connection at process exit costs nothing. A faulting process costs the log."""
    core = await _started(tmp_path, monkeypatch)
    connection = core.db.connection
    # Exactly what a drain timeout reports: "I gave up, entries may still be in flight."
    monkeypatch.setattr(core.security, "close", lambda: False)

    await asyncio.wait_for(core.stop(), timeout=30)

    assert _usable(connection), "closing it here is the access violation this test exists for"
    connection.close()

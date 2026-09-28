"""nox.core.boot.persistence: a damaged database is set aside; migrations follow a backup."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nox.core.boot.persistence import DB_BACKUPS_KEEP, open_database
from nox.data.db import Database

T0 = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)


def _existing_database(path: Path) -> None:
    """A fully migrated database with one row of user data, closed again."""
    db = Database(path)
    db.migrate()
    db.execute(
        "INSERT INTO sessions (id, started_at, mode, privacy_mode) VALUES ('s1', 'x', 'm', 'p')"
    )
    db.close()


def _make_last_migration_pending(path: Path) -> str:
    """Forget that the newest migration ran, so the next open has one to apply."""
    db = Database(path)
    row = db.fetch_one("SELECT id, name FROM schema_migrations ORDER BY id DESC LIMIT 1")
    assert row is not None
    db.execute("DELETE FROM schema_migrations WHERE id = ?", (row["id"],))
    db.close()
    return f"{int(row['id']):04d}_{row['name']}"


def test_a_damaged_header_is_set_aside_and_boot_continues_with_an_empty_database(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nox.db"
    _existing_database(path)
    raw = bytearray(path.read_bytes())
    raw[0:16] = b"this is garbage!"  # the SQLite magic header
    path.write_bytes(bytes(raw))
    path.with_name("nox.db-wal").write_bytes(b"recent commits")

    opened = open_database(path, backups_dir=tmp_path / "backups", now=lambda: T0)

    assert opened.recovery is not None
    assert opened.recovery.moved_to.name == "nox.db.corrupt-20260928120000"
    assert opened.recovery.moved_to.read_bytes()[:16] == b"this is garbage!"
    assert "cannot open" in opened.recovery.reason
    # The WAL belongs to the damaged file: it moves with it instead of being discarded when the
    # fresh database opens under the old name.
    moved_wal = opened.recovery.moved_to.with_name("nox.db.corrupt-20260928120000-wal")
    assert moved_wal.read_bytes() == b"recent commits"
    assert "nox.db.corrupt-20260928120000" in opened.recovery.describe()
    assert str(tmp_path) not in opened.recovery.describe()  # never a full path in health
    assert "sessions" in opened.db.table_names()
    assert opened.db.fetch_one("SELECT COUNT(*) FROM sessions")[0] == 0  # type: ignore[index]
    opened.db.close()


def test_a_failed_integrity_check_sets_the_database_aside(tmp_path: Path) -> None:
    path = tmp_path / "nox.db"
    db = Database(path)
    db.migrate()
    page_size = int(db.fetch_one("PRAGMA page_size")[0])  # type: ignore[index]
    rootpage = int(
        db.fetch_one("SELECT rootpage FROM sqlite_master WHERE name = 'tasks'")[0]  # type: ignore[index]
    )
    db.execute("PRAGMA journal_mode=DELETE")
    db.close()
    raw = bytearray(path.read_bytes())
    raw[(rootpage - 1) * page_size : rootpage * page_size] = b"\xff" * page_size
    path.write_bytes(bytes(raw))

    opened = open_database(path, now=lambda: T0)

    assert opened.recovery is not None
    assert opened.recovery.reason == "integrity check failed"
    assert opened.recovery.moved_to.exists()
    assert opened.db.integrity_check()
    opened.db.close()


def test_a_locked_or_unopenable_database_is_never_renamed_aside(tmp_path: Path) -> None:
    path = tmp_path / "nox.db"
    path.mkdir()  # sqlite3 cannot open a directory: an operational error, not damage

    with pytest.raises(sqlite3.OperationalError):
        open_database(path, now=lambda: T0)

    assert path.is_dir()
    assert not list(tmp_path.glob("*.corrupt-*"))


def test_pending_migrations_are_preceded_by_a_backup_with_the_old_data(tmp_path: Path) -> None:
    path = tmp_path / "nox.db"
    backups = tmp_path / "backups"
    _existing_database(path)
    pending = _make_last_migration_pending(path)

    opened = open_database(path, backups_dir=backups, now=lambda: T0)
    opened.db.close()

    assert opened.backup is not None
    assert opened.backup.name == f"nox-20260928120000-before-{pending}.db"
    copy = sqlite3.connect(opened.backup)
    assert copy.execute("SELECT id FROM sessions").fetchall() == [("s1",)]
    copy.close()


def test_only_the_newest_backups_are_kept(tmp_path: Path) -> None:
    path = tmp_path / "nox.db"
    backups = tmp_path / "backups"
    _existing_database(path)
    for day in range(DB_BACKUPS_KEEP + 2):
        _make_last_migration_pending(path)
        when = T0 + timedelta(days=day)
        open_database(path, backups_dir=backups, now=lambda when=when: when).db.close()

    kept = sorted(p.name for p in backups.iterdir())
    assert len(kept) == DB_BACKUPS_KEEP
    assert kept[0].startswith("nox-20260930")  # the two oldest are gone


def test_a_fresh_database_needs_no_backup(tmp_path: Path) -> None:
    opened = open_database(tmp_path / "nox.db", backups_dir=tmp_path / "backups", now=lambda: T0)
    opened.db.close()

    assert opened.backup is None
    assert opened.recovery is None
    assert not (tmp_path / "backups").exists()


def test_a_healthy_open_leaves_no_preserved_wal_copy_behind(tmp_path: Path) -> None:
    path = tmp_path / "nox.db"
    _existing_database(path)
    path.with_name("nox.db-wal").write_bytes(b"")  # left over by an unclean shutdown

    opened = open_database(path, now=lambda: T0)
    opened.db.close()

    assert opened.recovery is None
    assert not list(tmp_path.glob("*.preserved"))

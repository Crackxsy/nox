"""nox.data.db: pragmas, migrations, transactions, FTS5, integrity, sqlite-vec."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from nox.data.db import MIGRATIONS_DIR, Database, MigrationError, split_statements

EXPECTED_TABLES = {
    "schema_migrations",
    "state_checkpoints",
    "audit_log",
    "sessions",
    "turns",
    "memory_items",
    "memory_items_fts",
    "vault_index",
    "vault_chunks",
    "vault_chunks_fts",
    "health_history",
    "temporary_grants",
    "tasks",
    "paired_devices",
    "stream_sessions",
    "viewers",
    "viewer_memory",
    "chat_events",
    "funken_ledger",
    "moderation_actions",
    "minigame_sessions",
}


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "sub" / "nox.db")
    yield database
    database.close()


def test_pragmas(db: Database) -> None:
    assert db.fetch_one("PRAGMA journal_mode")[0] == "wal"  # type: ignore[index]
    assert db.fetch_one("PRAGMA foreign_keys")[0] == 1  # type: ignore[index]
    assert db.path.parent.is_dir()


def test_migrate_creates_all_tables_and_is_idempotent(db: Database) -> None:
    # Every release adds migrations, so this asserts the invariants (order, idempotence, base
    # tables) rather than a frozen list that any new `NNNN_*.sql` would break.
    applied = db.migrate()
    assert applied[:2] == ["0001_initial", "0002_stream"]
    assert applied == sorted(applied)
    assert EXPECTED_TABLES <= set(db.table_names())
    assert "memory_vec" not in db.table_names()
    assert db.migrate() == []
    assert db.applied_migrations() == sorted(db.applied_migrations())
    assert db.applied_migrations()[:2] == [1, 2]
    assert db.integrity_check() is True


def test_migration_applies_in_order_and_records(tmp_path: Path, db: Database) -> None:
    mig = tmp_path / "migs"
    mig.mkdir()
    (mig / "0002_second.sql").write_text("CREATE TABLE t2 (x INTEGER);", encoding="utf-8")
    (mig / "0001_first.sql").write_text(
        "CREATE TABLE t1 (x INTEGER);\nINSERT INTO t1 VALUES (1);\n", encoding="utf-8"
    )
    assert db.migrate(mig) == ["0001_first", "0002_second"]
    assert db.fetch_one("SELECT x FROM t1")[0] == 1  # type: ignore[index]
    rows = db.fetch_all("SELECT id, name FROM schema_migrations ORDER BY id")
    assert [(r["id"], r["name"]) for r in rows] == [(1, "first"), (2, "second")]


def test_failed_migration_rolls_back(tmp_path: Path, db: Database) -> None:
    mig = tmp_path / "migs"
    mig.mkdir()
    (mig / "0001_bad.sql").write_text(
        "CREATE TABLE good (x INTEGER);\nCREATE TABLE broken (;\n", encoding="utf-8"
    )
    with pytest.raises(MigrationError):
        db.migrate(mig)
    assert "good" not in db.table_names()
    assert db.applied_migrations() == []


def test_bad_migration_filenames(tmp_path: Path, db: Database) -> None:
    mig = tmp_path / "migs"
    mig.mkdir()
    (mig / "init.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match="NNNN_name"):
        db.migrate(mig)


def test_split_statements_handles_triggers() -> None:
    script = (MIGRATIONS_DIR / "0001_initial.sql").read_text(encoding="utf-8")
    statements = split_statements(script)
    triggers = [s for s in statements if s.startswith("CREATE TRIGGER")]
    assert len(triggers) == 6
    assert all(s.rstrip().endswith("END;") for s in triggers)
    with pytest.raises(MigrationError, match="incomplete"):
        split_statements("CREATE TABLE x (")


def test_transaction_commit_rollback_and_nesting(db: Database) -> None:
    db.execute("CREATE TABLE t (x INTEGER)")
    with db.transaction():
        db.execute("INSERT INTO t VALUES (1)")
    with pytest.raises(RuntimeError), db.transaction():
        db.execute("INSERT INTO t VALUES (2)")
        raise RuntimeError("abort")
    with db.transaction():
        db.execute("INSERT INTO t VALUES (3)")
        with pytest.raises(ValueError), db.transaction():
            db.execute("INSERT INTO t VALUES (4)")
            raise ValueError("inner")
        db.execute("INSERT INTO t VALUES (5)")
    assert [r[0] for r in db.fetch_all("SELECT x FROM t ORDER BY x")] == [1, 3, 5]
    assert db.fetch_one("SELECT x FROM t WHERE x = 99") is None


def test_foreign_keys_enforced(db: Database) -> None:
    db.migrate()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO turns (session_id, ts, role, text) VALUES ('missing', 'now', 'user', 'hi')"
        )


def test_fts5_stays_in_sync(db: Database) -> None:
    db.migrate()
    db.execute(
        "INSERT INTO memory_items (id, type, text, created_at) "
        "VALUES (1, 'fact', 'User mag Rocket League', 'now')"
    )
    hit = db.fetch_one("SELECT rowid FROM memory_items_fts WHERE memory_items_fts MATCH 'rocket'")
    assert hit is not None and hit[0] == 1
    db.execute("UPDATE memory_items SET text = 'User mag Delphi' WHERE id = 1")
    assert (
        db.fetch_one("SELECT rowid FROM memory_items_fts WHERE memory_items_fts MATCH 'rocket'")
        is None
    )
    assert db.fetch_one("SELECT rowid FROM memory_items_fts WHERE memory_items_fts MATCH 'delphi'")
    db.execute("DELETE FROM memory_items WHERE id = 1")
    assert (
        db.fetch_one("SELECT rowid FROM memory_items_fts WHERE memory_items_fts MATCH 'delphi'")
        is None
    )

    db.execute(
        "INSERT INTO vault_index (path, mtime, hash, indexed_at) VALUES ('a.md', 1, 'h', 'now')"
    )
    db.execute(
        "INSERT INTO vault_chunks (path, ord, text, hash) "
        "VALUES ('a.md', 0, 'chunk about obs', 'h')"
    )
    assert db.fetch_one("SELECT rowid FROM vault_chunks_fts WHERE vault_chunks_fts MATCH 'obs'")
    db.execute("DELETE FROM vault_index WHERE path = 'a.md'")  # cascades to chunks
    assert db.fetch_one("SELECT COUNT(*) FROM vault_chunks")[0] == 0  # type: ignore[index]


def test_sqlite_vec_optional(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    db.migrate()
    loaded = db.load_sqlite_vec()
    if loaded:
        assert db.vec_loaded and db.ensure_memory_vec(4) is True
        assert "memory_vec" in db.table_names()
        db.execute(
            "INSERT INTO memory_vec (rowid, embedding) VALUES (1, ?)", (b"\x00\x00\x80\x3f" * 4,)
        )
        row = db.fetch_one(
            "SELECT rowid FROM memory_vec WHERE embedding MATCH ? AND k = 1",
            (b"\x00\x00\x80\x3f" * 4,),
        )
        assert row is not None and row[0] == 1
    else:
        assert db.ensure_memory_vec() is False

    # missing package -> False, never raises
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "sqlite_vec":
            raise ImportError("gone")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    fresh = Database(":memory:")
    assert fresh.load_sqlite_vec() is False
    assert fresh.ensure_memory_vec() is False
    fresh.close()


def test_integrity_check_detects_corruption(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.db"
    db = Database(path)
    db.migrate()
    page_size = db.fetch_one("PRAGMA page_size")[0]  # type: ignore[index]
    rootpage = db.fetch_one("SELECT rootpage FROM sqlite_master WHERE name = 'tasks'")[0]  # type: ignore[index]
    db.execute("PRAGMA journal_mode=DELETE")
    db.close()
    # Clobber a whole non-schema page (the `tasks` table's own page) rather than page 1: page 1
    # holds the schema and is re-read while merely reopening the connection (below), so corrupting
    # it either heals itself (stale freed cell copies read back fine) or breaks the reopen itself
    # before integrity_check() ever runs. A dedicated table page isn't touched by opening/pragmas.
    raw = bytearray(path.read_bytes())
    offset = (int(rootpage) - 1) * int(page_size)
    raw[offset : offset + int(page_size)] = b"\xff" * int(page_size)
    path.write_bytes(bytes(raw))
    broken = Database(path)
    assert broken.integrity_check() is False
    broken.close()

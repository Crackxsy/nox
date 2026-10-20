"""`nox.security.pin_attempts`: the failure counter shares the core's connection safely."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nox.data.db import Database
from nox.security.pin_attempts import PinAttemptState, SqlitePinAttemptStore


def test_the_counter_survives_a_new_store_on_the_same_database(tmp_path: Path) -> None:
    db = Database(tmp_path / "nox.db")
    until = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
    SqlitePinAttemptStore(db.connection, lock=db.lock).save(PinAttemptState(3, until))
    again = SqlitePinAttemptStore(db.connection, lock=db.lock)
    assert again.load() == PinAttemptState(failed=3, locked_until=until)
    again.clear()
    assert again.load() == PinAttemptState()
    db.close()


def test_a_save_inside_another_transaction_does_not_commit_it(tmp_path: Path) -> None:
    """The store used to call COMMIT on the shared connection - ending whatever transaction was
    open on it, so that transaction's rollback (or commit) then failed."""
    db = Database(tmp_path / "nox.db")
    db.execute("CREATE TABLE notes (x INTEGER)")
    store = SqlitePinAttemptStore(db.connection, lock=db.lock)
    with pytest.raises(RuntimeError, match="abort"), db.transaction():
        db.execute("INSERT INTO notes VALUES (1)")
        store.save(PinAttemptState(failed=1))
        raise RuntimeError("abort")
    assert db.fetch_one("SELECT count(*) FROM notes")[0] == 0  # type: ignore[index]
    db.close()


def test_a_save_from_another_thread_waits_for_a_transaction_on_the_shared_connection(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "nox.db")
    db.execute("CREATE TABLE notes (x INTEGER)")
    store = SqlitePinAttemptStore(db.connection, lock=db.lock)
    saver = threading.Thread(target=store.save, args=(PinAttemptState(failed=2),))
    with db.transaction():
        db.execute("INSERT INTO notes VALUES (1)")
        saver.start()
        saver.join(0.3)
        # It must still be waiting: a store with a lock of its own wrote (and committed) here.
        assert saver.is_alive()
        db.execute("INSERT INTO notes VALUES (2)")
    saver.join(5)
    assert not saver.is_alive()
    assert db.fetch_one("SELECT count(*) FROM notes")[0] == 2  # type: ignore[index]
    assert store.load().failed == 2
    db.close()

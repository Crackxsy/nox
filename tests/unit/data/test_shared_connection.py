"""The audit log shares the core's one SQLite connection, so it must share its lock too."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from nox.data.db import Database
from nox.security.audit import SqliteAuditLog


class _AbortError(Exception):
    pass


def test_an_audit_write_cannot_commit_another_threads_open_transaction(tmp_path: Path) -> None:
    """With a lock of its own, the audit writer's COMMIT landed inside a transaction another thread
    had open on the same connection: half of that transaction became permanent (or the commit
    failed with "cannot commit - SQL statements in progress")."""
    db = Database(tmp_path / "nox.db")
    try:
        db.execute("CREATE TABLE work (step INTEGER)")
        audit = SqliteAuditLog(db.connection, lock=db.lock)
        written = threading.Event()

        def write_audit() -> None:
            audit.append(actor="t", tool="t", action="x", target="", decision="allow", result="ok")
            written.set()

        writer = threading.Thread(target=write_audit)
        with pytest.raises(_AbortError), db.transaction() as conn:
            conn.execute("INSERT INTO work VALUES (1)")
            writer.start()
            # The writer has to wait for this transaction instead of committing into it.
            assert not written.wait(0.3)
            raise _AbortError  # roll the half-done transaction back

        writer.join(5.0)
        assert written.is_set()
        assert db.fetch_all("SELECT step FROM work") == []  # nothing of the rolled-back work
        assert audit.verify_chain()
        assert len(audit.entries()) == 1
    finally:
        db.close()

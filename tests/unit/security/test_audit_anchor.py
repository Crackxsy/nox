"""The audit chain is complete, not merely consistent.

A hash chain cannot see rows that are gone. These tests remove them the way an attacker (or an
accident) would - drop the append-only trigger and delete from the end, delete below the
checkpoint, replace the whole database - and expect the boot check to call each one a broken
chain, with a reason. A fresh install, with no anchor anywhere yet, must pass.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from nox.core.config import NoxConfig
from nox.security.audit import (
    BREAK_ANCHOR_UNREADABLE,
    BREAK_CHAIN,
    BREAK_DELETED,
    BREAK_REWRITTEN,
    BREAK_ROLLBACK,
    BREAK_TRUNCATED,
    SqliteAuditLog,
)
from nox.security.audit_anchor import (
    AUDIT_HEAD_SECRET,
    AuditHead,
    FileAuditAnchor,
    SecretAuditAnchor,
    anchor_file_name,
)
from nox.security.secrets import InMemorySecretStore, SecretStoreUnavailableError
from nox.security.service import SecurityContext

from .conftest import DEFAULTS_YAML, PROFILES_DIR


class Store:
    """One audit database on disk, reopened as often as a test likes."""

    def __init__(self, root: Path) -> None:
        self.db_path = root / "db" / "nox.db"
        self.db_path.parent.mkdir(parents=True)
        self.anchor_path = root / "runtime" / anchor_file_name(self.db_path)
        self.secrets = InMemorySecretStore()
        self._conns: list[sqlite3.Connection] = []

    def open(self) -> SqliteAuditLog:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conns.append(conn)
        return SqliteAuditLog(conn)

    def anchors(self) -> tuple[FileAuditAnchor, SecretAuditAnchor]:
        return (
            FileAuditAnchor(self.anchor_path, database=self.db_path),
            SecretAuditAnchor(self.secrets, database=self.db_path),
        )

    def raw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        self._conns.append(conn)
        return conn

    def close(self) -> None:
        for conn in self._conns:
            conn.close()
        self._conns.clear()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path)
    yield s
    s.close()


def _append(audit: SqliteAuditLog, n: int) -> None:
    for i in range(n):
        audit.append(
            actor="test", tool="t", action=f"a{i}", target="", decision="allow", result="ok"
        )


def _first_session(store: Store, rows: int = 5) -> SqliteAuditLog:
    """Boot once cleanly (which arms the file anchor), write `rows` rows, anchor at shutdown."""
    audit = store.open()
    assert audit.verify_since_checkpoint(store.anchors()).ok
    audit.arm_anchor(store.anchors()[0])
    _append(audit, rows)
    audit.anchor_now(store.anchors())
    return audit


def _delete(store: Store, where: str) -> None:
    conn = store.raw()
    conn.execute("DROP TRIGGER audit_log_no_delete")
    conn.execute(f"DELETE FROM audit_log WHERE {where}")  # noqa: S608 - test-only literal
    conn.commit()


def test_a_fresh_install_has_no_anchor_and_passes(store: Store) -> None:
    audit = store.open()
    verification = audit.verify_since_checkpoint(store.anchors())
    assert verification.ok
    assert verification.anchors == {"file": "absent", "credential_store": "absent"}


def test_the_file_anchor_follows_every_appended_row(store: Store) -> None:
    audit = _first_session(store, rows=3)
    anchored = store.anchors()[0].read()
    assert anchored == audit.head() and anchored is not None and anchored.seq == 3


def test_an_intact_restart_passes_and_every_anchor_agrees(store: Store) -> None:
    _first_session(store)
    verification = store.open().verify_since_checkpoint(store.anchors())
    assert verification.ok
    assert verification.anchors == {"file": "ok", "credential_store": "ok"}


def test_tail_truncation_is_detected(store: Store) -> None:
    _first_session(store, rows=5)
    _delete(store, "seq > 3")

    verification = store.open().verify_since_checkpoint(store.anchors())

    assert not verification.ok
    assert verification.reason == BREAK_TRUNCATED
    assert verification.first_bad_seq == 4


def test_truncation_after_the_last_checkpoint_is_caught_by_the_file_anchor_alone(
    store: Store,
) -> None:
    """Rows written after the boot check exist only in the per-row file anchor."""
    audit = store.open()
    assert audit.verify_since_checkpoint(store.anchors()).ok  # checkpoint and anchors at 0
    audit.arm_anchor(store.anchors()[0])
    _append(audit, 4)
    _delete(store, "seq > 2")

    verification = store.open().verify_since_checkpoint(store.anchors())

    assert not verification.ok and verification.reason == BREAK_TRUNCATED
    assert verification.anchors["file"] == BREAK_TRUNCATED


def test_a_rollback_below_the_checkpoint_is_detected_and_the_checkpoint_does_not_move(
    store: Store,
) -> None:
    audit = store.open()
    _append(audit, 5)
    assert audit.verify_since_checkpoint().ok  # checkpoint at 5, no anchors at all
    _delete(store, "seq > 2")

    reopened = store.open()
    first = reopened.verify_since_checkpoint()
    second = reopened.verify_since_checkpoint()

    assert not first.ok and first.reason == BREAK_ROLLBACK
    assert not second.ok, "the checkpoint moved backwards and hid the rollback"
    row = store.raw().execute("SELECT seq FROM audit_verify_checkpoint").fetchone()
    assert row == (5,)


def test_a_deleted_database_is_detected(store: Store) -> None:
    _first_session(store, rows=5)
    store.close()
    store.db_path.unlink()
    for suffix in ("-wal", "-shm"):
        store.db_path.with_name(store.db_path.name + suffix).unlink(missing_ok=True)

    verification = store.open().verify_since_checkpoint(store.anchors())

    assert not verification.ok
    assert verification.reason == BREAK_DELETED


def test_a_deleted_database_and_anchor_file_is_caught_by_the_credential_store(
    store: Store,
) -> None:
    _first_session(store, rows=5)
    store.close()
    store.db_path.unlink()
    store.anchor_path.unlink()

    verification = store.open().verify_since_checkpoint(store.anchors())

    assert not verification.ok and verification.reason == BREAK_DELETED
    assert verification.anchors == {"file": "absent", "credential_store": BREAK_DELETED}


def test_a_rewritten_anchored_row_is_detected(store: Store) -> None:
    _first_session(store, rows=3)
    conn = store.raw()
    conn.execute("DROP TRIGGER audit_log_no_update")
    conn.execute("UPDATE audit_log SET hash = ? WHERE seq = 3", ("f" * 64,))
    conn.commit()

    verification = store.open().verify_since_checkpoint(store.anchors())

    assert not verification.ok
    assert verification.reason == BREAK_REWRITTEN
    assert verification.first_bad_seq == 3


def test_an_unreadable_anchor_is_a_break_not_a_fresh_install(store: Store) -> None:
    _first_session(store, rows=2)
    store.anchor_path.write_text("{not json", encoding="utf-8")

    verification = store.open().verify_since_checkpoint(store.anchors())

    assert not verification.ok and verification.reason == BREAK_ANCHOR_UNREADABLE


@pytest.mark.parametrize(
    "error",
    [SecretStoreUnavailableError("no backend"), RuntimeError("keychain is locked")],
    ids=["no-backend", "backend-error"],
)
def test_an_unavailable_credential_store_is_skipped_and_reported(
    store: Store, error: Exception
) -> None:
    class NoCredentialStore(InMemorySecretStore):
        def get(self, name: str) -> str | None:
            raise error

        def set(self, name: str, value: str) -> None:
            raise error

    audit = store.open()
    anchors = (
        FileAuditAnchor(store.anchor_path, database=store.db_path),
        SecretAuditAnchor(NoCredentialStore(), database=store.db_path),
    )
    verification = audit.verify_since_checkpoint(anchors)

    assert verification.ok
    assert verification.anchors == {"file": "absent", "credential_store": "unavailable"}


def test_an_anchor_of_another_database_is_not_evidence_about_this_one(store: Store) -> None:
    other = SecretAuditAnchor(store.secrets, database=store.db_path.with_name("elsewhere.db"))
    other.write(AuditHead(seq=99, hash="a" * 64))

    verification = store.open().verify_since_checkpoint(store.anchors())

    assert verification.ok
    assert store.secrets.get(AUDIT_HEAD_SECRET) is not None


def test_a_forward_walk_break_still_reports_the_chain_reason(store: Store) -> None:
    audit = _first_session(store, rows=3)
    conn = store.raw()
    conn.execute("DROP TRIGGER audit_log_no_update")
    conn.execute("UPDATE audit_log SET actor = 'evil' WHERE seq = 3")
    conn.commit()
    del audit

    verification = store.open().verify_since_checkpoint(store.anchors())

    assert not verification.ok and verification.reason == BREAK_CHAIN


def test_acknowledging_a_break_records_it_and_the_next_boot_passes(store: Store) -> None:
    _first_session(store, rows=5)
    _delete(store, "seq > 3")
    audit = store.open()
    verification = audit.verify_since_checkpoint(store.anchors())
    assert not verification.ok

    seq = audit.acknowledge_break(
        by="dashboard", verification=verification, anchors=store.anchors()
    )

    assert audit.details(seq) == {"reason": BREAK_TRUNCATED, "first_bad_seq": "4"}
    assert store.open().verify_since_checkpoint(store.anchors()).ok


# ---- through the security core --------------------------------------------------------------


def _config() -> NoxConfig:
    return NoxConfig.model_validate(yaml.safe_load(DEFAULTS_YAML.read_text(encoding="utf-8")))


def _boot(store: Store, runtime: Path) -> SecurityContext:
    conn = sqlite3.connect(store.db_path, check_same_thread=False)
    store._conns.append(conn)
    return SecurityContext.build(
        _config(),
        conn=conn,
        profiles_dir=PROFILES_DIR,
        secret_store=store.secrets,
        audit_anchor_dir=runtime,
        database_path=store.db_path,
    )


def test_the_security_core_detects_a_replaced_database_and_acknowledges_it(
    store: Store, tmp_path: Path
) -> None:
    runtime = tmp_path / "runtime"
    first = _boot(store, runtime)
    assert first.verify_boot().ok
    first.audit_store.append(
        actor="a", tool="t", action="x", target="", decision="allow", result="ok"
    )
    assert first.close()
    store.close()
    store.db_path.unlink()

    second = _boot(store, runtime)
    verification = second.verify_boot()
    try:
        assert not verification.ok and verification.reason == BREAK_DELETED
        assert second.audit_break is verification
        assert second.audit_store.entries()[-1].action == "audit.verify"

        assert second.acknowledge_audit_break(by="dashboard") is True
        assert second.audit_break is None
        assert second.acknowledge_audit_break(by="dashboard") is False
    finally:
        assert second.close()

    third = _boot(store, runtime)
    try:
        assert third.verify_boot().ok
    finally:
        assert third.close()


def test_the_anchor_is_not_moved_while_a_break_is_unacknowledged(
    store: Store, tmp_path: Path
) -> None:
    runtime = tmp_path / "runtime"
    first = _boot(store, runtime)
    assert first.verify_boot().ok
    for _ in range(3):
        first.audit_store.append(
            actor="a", tool="t", action="x", target="", decision="allow", result="ok"
        )
    assert first.close()
    _delete(store, "seq > 1")

    second = _boot(store, runtime)
    assert not second.verify_boot().ok
    assert second.close()  # shutdown after a break must not re-anchor to the shorter chain

    third = _boot(store, runtime)
    try:
        assert not third.verify_boot().ok, "the break was forgotten across a restart"
    finally:
        assert third.close()


def test_shutdown_without_a_boot_check_does_not_move_the_anchors(
    store: Store, tmp_path: Path
) -> None:
    runtime = tmp_path / "runtime"
    first = _boot(store, runtime)
    assert first.verify_boot().ok
    for _ in range(3):
        first.audit_store.append(
            actor="a", tool="t", action="x", target="", decision="allow", result="ok"
        )
    assert first.close()
    _delete(store, "seq > 1")

    unchecked = _boot(store, runtime)  # e.g. a boot that failed before its audit check
    assert unchecked.close()

    after = _boot(store, runtime)
    try:
        assert not after.verify_boot().ok
    finally:
        assert after.close()

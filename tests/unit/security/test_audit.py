"""Audit log: hash chain, tamper detection, append-only triggers, redaction, events."""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from nox.core.events import E
from nox.security.audit import GENESIS_HASH, REDACTED, SqliteAuditLog
from tests.unit.fakes import FakeBus


def _append_three(audit: SqliteAuditLog) -> None:
    audit.append(
        actor="nox.chat",
        tool="memory",
        action="write",
        target="note",
        decision="allow",
        result="ok",
    )
    audit.append(
        actor="nox.chat",
        tool="obs",
        action="scene.switch",
        target="Live",
        decision="confirm",
        result="pending",
        task_id="t1",
        details={"rule_id": "stream.obs.scene_switch"},
    )
    audit.append(
        actor="plugin:x",
        tool="game.input",
        action="send",
        target="",
        decision="deny",
        result="denied",
    )


def test_chain_links_and_verifies(audit: SqliteAuditLog) -> None:
    _append_three(audit)
    entries = audit.entries()
    assert [e.seq for e in entries] == [1, 2, 3]
    assert entries[0].prev_hash == GENESIS_HASH
    assert entries[1].prev_hash == entries[0].hash and entries[2].prev_hash == entries[1].hash
    v = audit.verify_chain_detailed()
    assert v.ok and v.checked == 3 and v.first_bad_seq is None
    assert audit.verify_chain() is True


def test_hash_is_sha256_over_canonical_json_plus_prev_hash(
    conn: sqlite3.Connection, audit: SqliteAuditLog
) -> None:
    _append_three(audit)
    row = conn.execute(
        "SELECT seq, ts, actor, tool, action, target, decision, result, task_id, details_json,"
        " prev_hash, hash FROM audit_log WHERE seq = 2"
    ).fetchone()
    entry = {
        "seq": row[0],
        "ts": row[1],
        "actor": row[2],
        "tool": row[3],
        "action": row[4],
        "target": row[5],
        "decision": row[6],
        "result": row[7],
        "task_id": row[8],
        "details_json": row[9],
    }
    canonical = json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert hashlib.sha256((canonical + row[10]).encode()).hexdigest() == row[11]


def test_append_only_triggers_block_update_and_delete(
    conn: sqlite3.Connection, audit: SqliteAuditLog
) -> None:
    _append_three(audit)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE audit_log SET actor = 'x' WHERE seq = 1")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM audit_log WHERE seq = 1")
    assert audit.verify_chain()


@pytest.mark.parametrize(
    "statement,bad_seq",
    [
        ("UPDATE audit_log SET decision = 'allow' WHERE seq = 3", 3),
        ("UPDATE audit_log SET actor = 'someone' WHERE seq = 2", 2),
        ("UPDATE audit_log SET prev_hash = '11' WHERE seq = 2", 2),
        ("DELETE FROM audit_log WHERE seq = 2", 3),
    ],
)
def test_tampering_after_dropping_triggers_is_detected(
    conn: sqlite3.Connection,
    audit: SqliteAuditLog,
    statement: str,
    bad_seq: int,
) -> None:
    _append_three(audit)
    conn.execute("DROP TRIGGER audit_log_no_update")
    conn.execute("DROP TRIGGER audit_log_no_delete")
    conn.execute(statement)
    conn.commit()
    v = audit.verify_chain_detailed()
    assert not v.ok and v.first_bad_seq == bad_seq
    assert audit.verify_chain() is False


def test_rehashing_a_tampered_row_still_breaks_the_next_link(
    conn: sqlite3.Connection, audit: SqliteAuditLog
) -> None:
    _append_three(audit)
    conn.execute("DROP TRIGGER audit_log_no_update")
    row = dict(
        zip(
            (
                "seq",
                "ts",
                "actor",
                "tool",
                "action",
                "target",
                "decision",
                "result",
                "task_id",
                "details_json",
                "prev_hash",
                "hash",
            ),
            conn.execute("SELECT * FROM audit_log WHERE seq = 2").fetchone(),
            strict=True,
        )
    )
    row["decision"] = "allow"
    new_hash = SqliteAuditLog.compute_hash(row, str(row["prev_hash"]))
    conn.execute("UPDATE audit_log SET decision = 'allow', hash = ? WHERE seq = 2", (new_hash,))
    conn.commit()
    v = audit.verify_chain_detailed()
    assert not v.ok and v.first_bad_seq == 3


def test_details_are_redacted_for_secret_like_keys(audit: SqliteAuditLog) -> None:
    seq = audit.append(
        actor="a",
        tool="t",
        action="x",
        target="",
        decision="allow",
        result="ok",
        details={
            "token": "abc123",
            "api_key": "k",
            "twitch_secret": "s",
            "PIN": "1234",
            "rule_id": "r1",
            "value": "v",
        },
    )
    details = audit.details(seq)
    assert details["rule_id"] == "r1"
    for key in ("token", "api_key", "twitch_secret", "PIN", "value"):
        assert details[key] == REDACTED
    assert "abc123" not in json.dumps(details)


async def test_security_audit_event_is_emitted(audit: SqliteAuditLog, bus: FakeBus) -> None:
    from nox.security._events import drain_pending

    seq = audit.append(
        actor="a", tool="t", action="x", target="tg", decision="deny", result="denied"
    )
    await drain_pending()
    ev = [e for e in bus.published if e.name == E.SECURITY_AUDIT]
    assert len(ev) == 1 and ev[0].payload["seq"] == seq and ev[0].payload["result"] == "denied"
    assert "details" not in ev[0].payload


def test_standalone_creates_table_and_survives_reopen(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = tmp_path / "audit.sqlite"
    c1 = sqlite3.connect(db)
    a1 = SqliteAuditLog(c1)
    a1.append(actor="a", tool="t", action="x", target="", decision="allow", result="ok")
    c1.close()
    c2 = sqlite3.connect(db)
    a2 = SqliteAuditLog(c2)
    a2.append(actor="a", tool="t", action="y", target="", decision="allow", result="ok")
    assert a2.count() == 2 and a2.verify_chain()
    with pytest.raises(KeyError):
        a2.details(99)
    c2.close()

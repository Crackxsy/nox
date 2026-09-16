"""SQLite audit log: append-only, SHA-256 hash-chained (Security Model §8, ADR-011, Data Model
`audit_log`).

`hash = sha256(canonical_json(entry_without_hash_and_prev_hash) + prev_hash)`; the first entry
chains
to `GENESIS_HASH`. Triggers make UPDATE/DELETE fail so tampering needs a deliberate DDL change,
which
`verify_chain()` then detects. Details under secret-like keys are redacted before they are stored.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from nox.core.events import AuditEntry, E, EventBus
from nox.security._events import publish_nowait
from nox.security._logging import get_logger

log = get_logger(__name__)

GENESIS_HASH = "0" * 64
REDACTED = "<redacted>"
Clock = Callable[[], datetime]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    seq          INTEGER PRIMARY KEY,
    ts           TEXT    NOT NULL,
    actor        TEXT    NOT NULL,
    tool         TEXT    NOT NULL DEFAULT '',
    action       TEXT    NOT NULL,
    target       TEXT    NOT NULL DEFAULT '',
    decision     TEXT    NOT NULL,
    result       TEXT    NOT NULL,
    task_id      TEXT,
    details_json TEXT    NOT NULL DEFAULT '{}',
    prev_hash    TEXT    NOT NULL,
    hash         TEXT    NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""

_COLUMNS = (
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
)


class ChainVerification(BaseModel):
    model_config = ConfigDict(frozen=True)
    ok: bool
    first_bad_seq: int | None = None
    checked: int = 0


class SqliteAuditLog:
    """Implements `nox.security.model.AuditLog` on an injected connection (works standalone)."""

    _REDACT_KEYS: ClassVar[frozenset[str]] = frozenset(
        {
            "pin",
            "secret",
            "token",
            "password",
            "passwd",
            "pwd",
            "api_key",
            "apikey",
            "authorization",
            "auth",
            "cookie",
            "credential",
            "credentials",
            "private_key",
            "access_token",
            "refresh_token",
            "session_token",
            "client_secret",
            "stream_key",
            "value",
        }
    )
    _REDACT_SUFFIXES: ClassVar[tuple[str, ...]] = ("_token", "_secret", "_key", "_password", "_pin")

    def __init__(
        self, conn: sqlite3.Connection, *, bus: EventBus | None = None, clock: Clock | None = None
    ) -> None:
        self._conn = conn
        self._bus = bus
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ---- helpers ---------------------------------------------------------------------------------

    @staticmethod
    def canonical_json(data: Mapping[str, Any]) -> str:
        return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def compute_hash(cls, entry: Mapping[str, Any], prev_hash: str) -> str:
        payload = {k: v for k, v in entry.items() if k not in ("hash", "prev_hash")}
        return hashlib.sha256((cls.canonical_json(payload) + prev_hash).encode("utf-8")).hexdigest()

    @classmethod
    def _is_secret_key(cls, key: str) -> bool:
        k = key.strip().lower()
        return k in cls._REDACT_KEYS or k.endswith(cls._REDACT_SUFFIXES)

    @classmethod
    def redact(cls, details: Mapping[str, str] | None) -> dict[str, str]:
        if not details:
            return {}
        return {
            str(k): (REDACTED if cls._is_secret_key(str(k)) else str(v)) for k, v in details.items()
        }

    def _last(self) -> tuple[int, str]:
        row = self._conn.execute(
            "SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return 0, GENESIS_HASH
        return int(row[0]), str(row[1])

    # ---- AuditLog protocol -----------------------------------------------------------------------

    def append(
        self,
        *,
        actor: str,
        tool: str,
        action: str,
        target: str,
        decision: str,
        result: str,
        task_id: str | None = None,
        details: dict[str, str] | None = None,
    ) -> int:
        safe_details = self.redact(details)
        with self._lock:
            last_seq, prev_hash = self._last()
            seq = last_seq + 1
            entry: dict[str, Any] = {
                "seq": seq,
                "ts": self._clock().isoformat(),
                "actor": actor,
                "tool": tool,
                "action": action,
                "target": target,
                "decision": decision,
                "result": result,
                "task_id": task_id,
                "details_json": self.canonical_json(safe_details),
            }
            digest = self.compute_hash(entry, prev_hash)
            self._conn.execute(
                "INSERT INTO audit_log (seq, ts, actor, tool, action, target, decision, result,"
                " task_id, details_json, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    seq,
                    entry["ts"],
                    actor,
                    tool,
                    action,
                    target,
                    decision,
                    result,
                    task_id,
                    entry["details_json"],
                    prev_hash,
                    digest,
                ),
            )
            self._conn.commit()
        payload = AuditEntry(
            seq=seq,
            actor=actor,
            tool=tool,
            action=action,
            target=target,
            decision=decision,
            result=result,
            task_id=task_id,
            prev_hash=prev_hash,
            hash=digest,
        )
        publish_nowait(self._bus, E.SECURITY_AUDIT, payload)
        log.debug("security.audit_appended", seq=seq, actor=actor, action=action, decision=decision)
        return seq

    def verify_chain(self) -> bool:
        return self.verify_chain_detailed().ok

    def verify_chain_detailed(self) -> ChainVerification:
        """Walk every row in seq order; report the first row whose hash or prev_hash is wrong."""
        prev_hash = GENESIS_HASH
        expected_seq = 1
        checked = 0
        with self._lock:
            cursor = self._conn.execute(
                "SELECT " + ", ".join(_COLUMNS) + " FROM audit_log ORDER BY seq ASC"  # noqa: S608
            )
            for row in cursor:
                entry = dict(zip(_COLUMNS, row, strict=True))
                seq = int(entry["seq"])
                if seq != expected_seq or entry["prev_hash"] != prev_hash:
                    return ChainVerification(ok=False, first_bad_seq=seq, checked=checked)
                if self.compute_hash(entry, prev_hash) != entry["hash"]:
                    return ChainVerification(ok=False, first_bad_seq=seq, checked=checked)
                prev_hash = str(entry["hash"])
                expected_seq = seq + 1
                checked += 1
        return ChainVerification(ok=True, first_bad_seq=None, checked=checked)

    # ---- read access (dashboard, tests) ---------------------------------------------------------

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()
        return int(row[0]) if row else 0

    def entries(self, *, since_seq: int = 0, limit: int = 100) -> list[AuditEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, actor, tool, action, target, decision, result, task_id, prev_hash,"
                " hash FROM audit_log WHERE seq > ? ORDER BY seq ASC LIMIT ?",
                (since_seq, limit),
            ).fetchall()
        return [
            AuditEntry(
                seq=r[0],
                actor=r[1],
                tool=r[2],
                action=r[3],
                target=r[4],
                decision=r[5],
                result=r[6],
                task_id=r[7],
                prev_hash=r[8],
                hash=r[9],
            )
            for r in rows
        ]

    def details(self, seq: int) -> dict[str, str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT details_json FROM audit_log WHERE seq = ?", (seq,)
            ).fetchone()
        if row is None:
            raise KeyError(seq)
        loaded: dict[str, str] = json.loads(row[0])
        return loaded

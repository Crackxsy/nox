"""The audit log: append-only, SHA-256 hash-chained, stored in SQLite.

`hash = sha256(canonical_json(entry_without_hash_and_prev_hash) + prev_hash)`, and the first entry
chains to `GENESIS_HASH`. Triggers make UPDATE and DELETE fail, so tampering needs a deliberate
schema change - which `verify_chain()` then detects, because every later row's hash depends on the
one before it.

Details are redacted before they are stored: any key that names a secret is replaced, at every
nesting level, and a nested structure is stored as redacted JSON rather than as `str(dict)`.

Verification comes in two shapes. `verify_chain_detailed()` walks every row and is what a
user-triggered check runs. Boot uses `verify_since_checkpoint()`, which walks forward from the last
verified position and records a new checkpoint: the first boot on a database verifies everything,
every later boot verifies only what was written since, so a log with two years of retention does
not turn startup into a full-table scan.

A valid chain can still be a *shorter* chain, so the boot check also looks backwards:

- the checkpoint only ever moves forward, and a head behind it, or a checkpoint row whose hash
  changed, is a rollback;
- the head is anchored outside the database (`nox.security.audit_anchor`); a head behind an
  anchor - rows deleted from the end, or a whole new database - or an anchored row with another
  hash is a truncation or a rewrite.

Any of these is a broken chain, reported with a reason, and nothing is moved: neither the
checkpoint nor an anchor is updated past a break, so the evidence stays until a person
acknowledges it (`acknowledge_break`, which the core calls on a successful resume).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from nox.core.events import AuditEntry, E, EventBus
from nox.security._events import publish_nowait
from nox.security._logging import get_logger
from nox.security.audit_anchor import (
    AnchorUnavailableError,
    AnchorUnreadableError,
    AuditAnchor,
    AuditHead,
)

log = get_logger(__name__)

GENESIS_HASH = "0" * 64
REDACTED = "<redacted>"
Clock = Callable[[], datetime]

#: How deep `redact` walks a nested detail value before it stores a marker instead. Audit details
#: are short by design; anything deeper is a caller passing a whole object by mistake.
_MAX_REDACT_DEPTH = 6

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
CREATE TABLE IF NOT EXISTS audit_verify_checkpoint (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    seq         INTEGER NOT NULL,
    hash        TEXT    NOT NULL,
    verified_at TEXT    NOT NULL
);
"""

_CHECKPOINT_FORCE_SQL = (
    "INSERT INTO audit_verify_checkpoint (id, seq, hash, verified_at) VALUES (1, ?, ?, ?)"
    " ON CONFLICT(id) DO UPDATE SET seq = excluded.seq, hash = excluded.hash,"
    " verified_at = excluded.verified_at"
)
#: The checkpoint only ever moves forward: an older head never overwrites a newer checkpoint.
_CHECKPOINT_FORWARD_SQL = (
    _CHECKPOINT_FORCE_SQL + " WHERE excluded.seq >= audit_verify_checkpoint.seq"
)

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
    #: Why the chain is broken: `chain` (a row does not link), `rollback` (the head or the
    #: checkpoint row went backwards), `truncated` / `deleted` (the head is behind an anchor),
    #: `rewritten` (an anchored row has another hash), `anchor_unreadable`. Empty when ok.
    reason: str = ""
    #: Per anchor: `ok`, `absent` (never anchored: a fresh install), `unavailable` (its storage
    #: cannot be reached), or the break it found.
    anchors: dict[str, str] = Field(default_factory=dict)


#: Break reasons, as reported in `ChainVerification.reason` and the boot's audit entry.
BREAK_CHAIN = "chain"
BREAK_ROLLBACK = "rollback"
BREAK_TRUNCATED = "truncated"
BREAK_DELETED = "deleted"
BREAK_REWRITTEN = "rewritten"
BREAK_ANCHOR_UNREADABLE = "anchor_unreadable"


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
        self,
        conn: sqlite3.Connection,
        *,
        bus: EventBus | None = None,
        clock: Clock | None = None,
        lock: threading.RLock | None = None,
    ) -> None:
        """`lock` must be the lock every other user of `conn` holds - `Database.lock` in the core.

        The audit writer runs on its own thread. With a lock of its own it could commit in the
        middle of another thread's transaction on the same connection, committing that one's
        half-written rows with it or failing with "cannot commit - SQL statements in progress".
        """
        self._conn = conn
        self._bus = bus
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = lock if lock is not None else threading.RLock()
        #: Updated after every append once `arm_anchor` was called (after a clean boot check).
        self._head_anchor: AuditAnchor | None = None
        #: Consecutive anchor writes that failed; the row itself was committed each time.
        self.anchor_failures = 0
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
    def redact(cls, details: Mapping[str, Any] | None) -> dict[str, str]:
        """Replace every secret-like value, at any nesting depth, and flatten to strings.

        A nested mapping used to be stored as `str(dict)`, which wrote `{'token': 'abc'}` into the
        log verbatim, because only the top-level keys were inspected. Nested structures are walked
        and re-serialised as JSON, so the redaction covers them and the stored value stays
        readable.
        """
        if not details:
            return {}
        return {str(key): cls._redact_value(str(key), value) for key, value in details.items()}

    @classmethod
    def _redact_value(cls, key: str, value: Any) -> str:
        if cls._is_secret_key(key):
            return REDACTED
        scrubbed = cls._scrub(value, 0)
        return scrubbed if isinstance(scrubbed, str) else cls.canonical_json_value(scrubbed)

    @classmethod
    def _scrub(cls, value: Any, depth: int) -> Any:
        if depth > _MAX_REDACT_DEPTH:
            return "[...]"
        if isinstance(value, Mapping):
            return {
                str(key): (
                    REDACTED if cls._is_secret_key(str(key)) else cls._scrub(item, depth + 1)
                )
                for key, item in value.items()
            }
        if isinstance(value, list | tuple | set | frozenset):
            return [cls._scrub(item, depth + 1) for item in value]
        return value if isinstance(value, str) else str(value)

    @staticmethod
    def canonical_json_value(value: Any) -> str:
        """A deterministic JSON rendering of one already-redacted detail value."""
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

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
            # Inside the lock, so anchor writes happen in sequence order.
            self._anchor_head(AuditHead(seq=seq, hash=digest))
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
        return self._verify_from(GENESIS_HASH, 1)

    def verify_since_checkpoint(self, anchors: Sequence[AuditAnchor] = ()) -> ChainVerification:
        """Verify everything written since the last successful check, then record a checkpoint.

        The first call on a database has no checkpoint and therefore verifies the whole chain.
        Later calls start from the recorded `(seq, hash)` pair, so a two-year retention window
        does not make every start scan the entire table. Before walking forward it looks back:
        the head must not be behind the checkpoint or any anchor, and the rows they name must
        still carry the hashes they recorded (see the module docstring). Only a clean result
        moves the checkpoint and the anchors forward.
        """
        with self._lock:
            head_seq = self._last()[0]
            checkpoint = self._checkpoint()
            broken = self._check_checkpoint(checkpoint, head_seq)
        statuses: dict[str, str] = {}
        for anchor in anchors:
            status, anchor_break = self._check_anchor(anchor, head_seq)
            statuses[anchor.name] = status
            broken = broken or anchor_break
        if broken is not None:
            return broken.model_copy(update={"anchors": statuses})
        start_hash, start_seq = checkpoint if checkpoint is not None else (GENESIS_HASH, 1)
        verification = self._verify_from(start_hash, start_seq)
        if not verification.ok:
            return verification.model_copy(update={"reason": BREAK_CHAIN, "anchors": statuses})
        self._write_checkpoint()
        self.anchor_now([a for a in anchors if statuses.get(a.name) != "unavailable"])
        return verification.model_copy(update={"anchors": statuses})

    def _check_checkpoint(
        self, checkpoint: tuple[str, int] | None, head_seq: int
    ) -> ChainVerification | None:
        """A head behind the checkpoint, or a checkpoint row with another hash, is a rollback."""
        if checkpoint is None:
            return None
        cp_hash, next_seq = checkpoint
        cp_seq = next_seq - 1
        if head_seq < cp_seq:
            return ChainVerification(ok=False, first_bad_seq=head_seq + 1, reason=BREAK_ROLLBACK)
        if self._hash_at(cp_seq) != cp_hash:
            return ChainVerification(ok=False, first_bad_seq=cp_seq, reason=BREAK_ROLLBACK)
        return None

    def _check_anchor(
        self, anchor: AuditAnchor, head_seq: int
    ) -> tuple[str, ChainVerification | None]:
        """`(status, break)` for one anchor; the break is None when the anchor agrees."""
        try:
            anchored = anchor.read()
        except AnchorUnavailableError as exc:
            log.warning("security.audit_anchor_unavailable", anchor=anchor.name, reason=str(exc))
            return "unavailable", None
        except AnchorUnreadableError as exc:
            log.critical("security.audit_anchor_unreadable", anchor=anchor.name, reason=str(exc))
            broken = ChainVerification(ok=False, reason=BREAK_ANCHOR_UNREADABLE)
            return BREAK_ANCHOR_UNREADABLE, broken
        if anchored is None:
            return "absent", None
        if head_seq < anchored.seq:
            reason = BREAK_DELETED if head_seq == 0 else BREAK_TRUNCATED
            return reason, ChainVerification(ok=False, first_bad_seq=head_seq + 1, reason=reason)
        if anchored.seq > 0:
            with self._lock:
                stored = self._hash_at(anchored.seq)
            if stored != anchored.hash:
                broken = ChainVerification(
                    ok=False, first_bad_seq=anchored.seq, reason=BREAK_REWRITTEN
                )
                return BREAK_REWRITTEN, broken
        return "ok", None

    def head(self) -> AuditHead:
        """The newest row's sequence number and hash (`0` and the genesis hash when empty)."""
        with self._lock:
            seq, digest = self._last()
        return AuditHead(seq=seq, hash=digest)

    def arm_anchor(self, anchor: AuditAnchor) -> None:
        """Keep `anchor` at the head after every append from now on.

        Armed only after a clean boot check: an anchor that followed the database before it was
        compared with it would simply agree with whatever the database says.
        """
        with self._lock:
            self._head_anchor = anchor
            self._anchor_head(self.head())

    def anchor_now(self, anchors: Sequence[AuditAnchor]) -> None:
        """Write the current head to `anchors` now (boot, acknowledgement and shutdown)."""
        head = self.head()
        for anchor in anchors:
            try:
                anchor.write(head)
            except AnchorUnavailableError as exc:
                log.warning(
                    "security.audit_anchor_unavailable", anchor=anchor.name, reason=str(exc)
                )
            except Exception as exc:  # noqa: BLE001 - a lagging anchor is logged, never fatal
                log.error(
                    "security.audit_anchor_write_failed",
                    anchor=anchor.name,
                    error=f"{type(exc).__name__}: {exc}",
                )

    def acknowledge_break(
        self, *, by: str, verification: ChainVerification, anchors: Sequence[AuditAnchor]
    ) -> int:
        """A person has seen the break and resumed: record that, and start from the head again.

        The acknowledgement is itself an audit entry naming the break, so what was detected stays
        in the log. Then the checkpoint is set to the new head - the one place it may move
        backwards, because the rows it described are gone - and every anchor follows. Returns the
        acknowledgement's sequence number.
        """
        seq = self.append(
            actor=by,
            tool="security",
            action="audit.break_acknowledged",
            target="",
            decision="allow",
            result="ok",
            details={
                "reason": verification.reason,
                "first_bad_seq": str(verification.first_bad_seq),
            },
        )
        self._write_checkpoint(force=True)
        self.anchor_now(anchors)
        log.warning("security.audit_break_acknowledged", by=by, reason=verification.reason)
        return seq

    def _anchor_head(self, head: AuditHead) -> None:
        """Move the armed anchor to `head`. A failure is logged: the row itself is committed."""
        anchor = self._head_anchor
        if anchor is None:
            return
        try:
            anchor.write(head)
        except Exception as exc:  # noqa: BLE001 - the anchor merely lags; never fail the append
            self.anchor_failures += 1
            log.error(
                "security.audit_anchor_write_failed",
                anchor=anchor.name,
                error=f"{type(exc).__name__}: {exc}",
                failures=self.anchor_failures,
            )
            return
        self.anchor_failures = 0

    def _hash_at(self, seq: int) -> str | None:
        row = self._conn.execute("SELECT hash FROM audit_log WHERE seq = ?", (seq,)).fetchone()
        return None if row is None else str(row[0])

    def _verify_from(self, prev_hash: str, expected_seq: int) -> ChainVerification:
        checked = 0
        with self._lock:
            cursor = self._conn.execute(
                # _COLUMNS is a module constant, never caller input.
                "SELECT " + ", ".join(_COLUMNS) + " FROM audit_log WHERE seq >= ? ORDER BY seq ASC",  # noqa: S608
                (expected_seq,),
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

    def _checkpoint(self) -> tuple[str, int] | None:
        """`(hash, next_seq)` to resume verification from, or None when nothing is recorded."""
        with self._lock:
            row = self._conn.execute(
                "SELECT seq, hash FROM audit_verify_checkpoint WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        return str(row[1]), int(row[0]) + 1

    def _write_checkpoint(self, *, force: bool = False) -> None:
        """Record the head as verified. Only ever forward, unless a break was acknowledged."""
        with self._lock:
            last_seq, last_hash = self._last()
            if last_seq == 0:
                return
            self._conn.execute(
                _CHECKPOINT_FORCE_SQL if force else _CHECKPOINT_FORWARD_SQL,
                (last_seq, last_hash, self._clock().isoformat()),
            )
            self._conn.commit()

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

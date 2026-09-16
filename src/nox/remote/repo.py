"""SQLite repository for the remote area (migration `0008_remote.sql`, Spec v0.8 §6).

Two hashing rules are enforced here and nowhere else:
  * a pairing code is stored as `sha256(salt + code)`,
  * a device key is stored as `sha256(key_salt + channel + ':' + sender_id)`.
Both are compared with `secrets.compare_digest`. Neither the code nor the transport identity is
ever written to a column, a log line or an audit entry (ENGINEERING.md: no secrets in logs).
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import uuid
from datetime import UTC, datetime

from nox.data.db import Database
from nox.remote.models import DeviceRow


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _dt(value: str | None) -> datetime | None:
    return None if not value else datetime.fromisoformat(value)


def hash_value(salt: str, value: str) -> str:
    """The one hashing primitive this area uses. SHA-256 over `salt + value`, hex encoded."""
    return hashlib.sha256(f"{salt}{value}".encode()).hexdigest()


def sender_ref(channel: str, sender_id: str) -> str:
    """A stable, non-reversible pseudonym for an *unpaired* sender, so a rejected attempt can be
    correlated in the audit trail without persisting the account id (Spec §5.3)."""
    return hashlib.sha256(f"{channel}:{sender_id}".encode()).hexdigest()[:12]


def _row_to_device(row: sqlite3.Row) -> DeviceRow:
    paired_at = _dt(row["paired_at"])
    if paired_at is None:  # pragma: no cover - NOT NULL in the schema
        raise ValueError("paired_devices.paired_at is NULL")
    return DeviceRow(
        id=str(row["id"]),
        name=str(row["name"]),
        channel=str(row["channel"]),
        role=str(row["role"]),
        paired_at=paired_at,
        last_seen_at=_dt(row["last_seen_at"]),
        revoked_at=_dt(row["revoked_at"]),
        revoked_reason=str(row["revoked_reason"]),
        last_update_id=int(row["last_update_id"]),
    )


class RemoteRepository:
    """Typed access to `paired_devices`, `remote_pairings` and `remote_audit`."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # -- pairings ----------------------------------------------------------------------------

    def create_pairing(
        self,
        code: str,
        *,
        channel: str,
        device_name: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> str:
        pairing_id = uuid.uuid4().hex
        salt = secrets.token_hex(16)
        self._db.execute(
            "INSERT INTO remote_pairings (id, code_hash, salt, channel, device_name, created_at,"
            " expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                pairing_id,
                hash_value(salt, code),
                salt,
                channel,
                device_name,
                _iso(created_at),
                _iso(expires_at),
            ),
        )
        return pairing_id

    def find_pairing(self, code: str, *, channel: str) -> sqlite3.Row | None:
        """The open pairing whose hash matches `code`, newest first. Redeemed rows are kept (audit
        trail) and matched too, so a re-used code is rejected as `used` rather than `unknown_code`.
        """
        rows = self._db.fetch_all(
            "SELECT * FROM remote_pairings WHERE channel = ? ORDER BY created_at DESC",
            (channel,),
        )
        for row in rows:
            if secrets.compare_digest(str(row["code_hash"]), hash_value(str(row["salt"]), code)):
                return row
        return None

    def mark_pairing_redeemed(self, pairing_id: str, device_id: str, when: datetime) -> bool:
        """Atomic redeem-once: the `redeemed_at IS NULL` guard means exactly one of two racing
        redemptions of the same code updates a row (ST-17-01)."""
        cursor = self._db.execute(
            "UPDATE remote_pairings SET redeemed_at = ?, device_id = ?"
            " WHERE id = ? AND redeemed_at IS NULL",
            (_iso(when), device_id, pairing_id),
        )
        return cursor.rowcount == 1

    def purge_expired_pairings(self, now: datetime) -> int:
        cursor = self._db.execute(
            "DELETE FROM remote_pairings WHERE redeemed_at IS NULL AND expires_at < ?",
            (_iso(now),),
        )
        return int(cursor.rowcount)

    # -- devices -----------------------------------------------------------------------------

    def create_device(
        self,
        *,
        name: str,
        channel: str,
        sender_id: str,
        pairing_code_hash: str,
        paired_at: datetime,
    ) -> DeviceRow:
        device_id = uuid.uuid4().hex
        key_salt = secrets.token_hex(16)
        self._db.execute(
            "INSERT INTO paired_devices (id, name, role, public_key, paired_at, channel,"
            " key_salt, pairing_code_hash) VALUES (?, ?, 'remote', ?, ?, ?, ?, ?)",
            (
                device_id,
                name,
                hash_value(key_salt, f"{channel}:{sender_id}"),
                _iso(paired_at),
                channel,
                key_salt,
                pairing_code_hash,
            ),
        )
        found = self.get_device(device_id)
        if found is None:  # pragma: no cover - the row was just inserted
            raise RuntimeError("paired device vanished right after insert")
        return found

    def get_device(self, device_id: str) -> DeviceRow | None:
        row = self._db.fetch_one("SELECT * FROM paired_devices WHERE id = ?", (device_id,))
        return None if row is None else _row_to_device(row)

    def list_devices(self, *, include_revoked: bool = True) -> list[DeviceRow]:
        sql = "SELECT * FROM paired_devices"
        if not include_revoked:
            sql += " WHERE revoked_at IS NULL"
        sql += " ORDER BY paired_at"
        return [_row_to_device(r) for r in self._db.fetch_all(sql)]

    def find_device_for_sender(self, channel: str, sender_id: str) -> DeviceRow | None:
        """Constant-time match of the hashed device key against every non-revoked device. A revoked
        device is never matched, so revocation takes effect on the very next message (ST-17-03)."""
        rows = self._db.fetch_all(
            "SELECT * FROM paired_devices WHERE channel = ? AND revoked_at IS NULL", (channel,)
        )
        probe = f"{channel}:{sender_id}"
        for row in rows:
            expected = hash_value(str(row["key_salt"]), probe)
            if secrets.compare_digest(str(row["public_key"]), expected):
                return _row_to_device(row)
        return None

    def revoke_device(self, device_id: str, *, reason: str, when: datetime) -> bool:
        cursor = self._db.execute(
            "UPDATE paired_devices SET revoked_at = ?, revoked_reason = ?"
            " WHERE id = ? AND revoked_at IS NULL",
            (_iso(when), reason, device_id),
        )
        return cursor.rowcount == 1

    def touch_device(self, device_id: str, *, when: datetime, update_id: int) -> None:
        """Record liveness and advance the replay watermark. `max()` keeps it monotonic even if two
        messages are processed out of order."""
        self._db.execute(
            "UPDATE paired_devices SET last_seen_at = ?, last_update_id = max(last_update_id, ?)"
            " WHERE id = ?",
            (_iso(when), update_id, device_id),
        )

    # -- audit -------------------------------------------------------------------------------

    def append_audit(
        self,
        *,
        ts: datetime,
        channel: str,
        sender_id: str,
        device_id: str,
        command: str,
        decision: str,
        reason: str = "",
    ) -> int:
        cursor = self._db.execute(
            "INSERT INTO remote_audit (ts, channel, sender_ref, device_id, command, decision,"
            " reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                _iso(ts),
                channel,
                sender_ref(channel, sender_id),
                device_id,
                command,
                decision,
                reason,
            ),
        )
        return int(cursor.lastrowid or 0)

    def audit_entries(self, limit: int = 100) -> list[sqlite3.Row]:
        return self._db.fetch_all(
            "SELECT * FROM remote_audit ORDER BY id DESC LIMIT ?", (int(limit),)
        )

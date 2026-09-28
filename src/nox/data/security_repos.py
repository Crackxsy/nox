"""The `security_state` table (migration `0011_security_state.sql`): one row, rewritten per change.

Implements `nox.security.persisted_state.SecurityStateStore`. The row is read once at boot and
written on every privacy-mode, panic and kill-switch change. A row that exists but cannot be read
back into a valid state raises `SecurityStateUnreadableError` - never "nothing stored", because
the boot treats the two very differently: nothing stored means a fresh install and the configured
values; unreadable means the strictest mode and safe mode.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from pydantic import ValidationError

from nox.core.state import PrivacyMode
from nox.data.db import Database
from nox.security.persisted_state import PersistedSecurityState, SecurityStateUnreadableError

_COLUMNS = (
    "privacy_mode",
    "panic",
    "kill_engaged",
    "kill_security_path",
    "kill_origin",
    "kill_reason",
)


class SecurityStateRepository:
    """Reads and writes the single `security_state` row."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def load(self) -> PersistedSecurityState | None:
        try:
            row = self._db.fetch_one(
                # _COLUMNS is a module constant, never caller input.
                "SELECT " + ", ".join(_COLUMNS) + " FROM security_state WHERE id = 1"  # noqa: S608
            )
        except sqlite3.Error as exc:
            raise SecurityStateUnreadableError(f"{type(exc).__name__}: {exc}") from exc
        if row is None:
            return None
        data = dict(zip(_COLUMNS, tuple(row), strict=True))
        try:
            # Lax validation turns the stored 0/1 into booleans; anything else is refused.
            return PersistedSecurityState.model_validate(data)
        except ValidationError as exc:
            raise SecurityStateUnreadableError(
                f"stored security state does not validate: {exc.errors()[0]['msg']}",
                privacy_mode=_parse_mode(data.get("privacy_mode")),
            ) from exc

    def save(self, state: PersistedSecurityState) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO security_state (id, privacy_mode, panic, kill_engaged,"
                " kill_security_path, kill_origin, kill_reason, updated_at)"
                " VALUES (1, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET privacy_mode = excluded.privacy_mode,"
                " panic = excluded.panic, kill_engaged = excluded.kill_engaged,"
                " kill_security_path = excluded.kill_security_path,"
                " kill_origin = excluded.kill_origin, kill_reason = excluded.kill_reason,"
                " updated_at = excluded.updated_at",
                (
                    state.privacy_mode.value,
                    int(state.panic),
                    int(state.kill_engaged),
                    int(state.kill_security_path),
                    state.kill_origin,
                    state.kill_reason,
                    datetime.now(UTC).isoformat(),
                ),
            )


def _parse_mode(value: object) -> PrivacyMode | None:
    try:
        return PrivacyMode(str(value))
    except ValueError:
        return None

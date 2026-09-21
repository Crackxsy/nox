"""Where failed PIN attempts are counted.

The count and the lockout deadline used to live in the `PinManager` instance, which meant they
were gone the moment the core restarted - and the supervisor restarts the core on request. Five
guesses per restart is not a lockout. The counter therefore lives in the same database as the
audit log and survives a restart; `InMemoryPinAttemptStore` is the equivalent for a test or for a
`PinManager` built without a database.

The table holds exactly one row. It is deliberately not part of the append-only audit log: it is
mutable state by nature, and the attempts themselves are audited separately.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

__all__ = [
    "InMemoryPinAttemptStore",
    "PinAttemptState",
    "PinAttemptStore",
    "SqlitePinAttemptStore",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pin_attempts (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    failed       INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT
);
"""


@dataclass(frozen=True, slots=True)
class PinAttemptState:
    """Consecutive failures, and when the lockout they caused ends."""

    failed: int = 0
    locked_until: datetime | None = None


class PinAttemptStore(Protocol):
    def load(self) -> PinAttemptState: ...
    def save(self, state: PinAttemptState) -> None: ...
    def clear(self) -> None: ...


class InMemoryPinAttemptStore:
    """Keeps the counter for the lifetime of the process. For tests and for offline tools."""

    def __init__(self) -> None:
        self._state = PinAttemptState()

    def load(self) -> PinAttemptState:
        return self._state

    def save(self, state: PinAttemptState) -> None:
        self._state = state

    def clear(self) -> None:
        self._state = PinAttemptState()


class SqlitePinAttemptStore:
    """Keeps the counter in the Nox database, so a restart does not reset a lockout."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def load(self) -> PinAttemptState:
        with self._lock:
            row = self._conn.execute(
                "SELECT failed, locked_until FROM pin_attempts WHERE id = 1"
            ).fetchone()
        if row is None:
            return PinAttemptState()
        locked_until = datetime.fromisoformat(row[1]) if row[1] else None
        return PinAttemptState(failed=int(row[0]), locked_until=locked_until)

    def save(self, state: PinAttemptState) -> None:
        locked = state.locked_until.isoformat() if state.locked_until is not None else None
        with self._lock:
            self._conn.execute(
                "INSERT INTO pin_attempts (id, failed, locked_until) VALUES (1, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET failed = excluded.failed,"
                " locked_until = excluded.locked_until",
                (state.failed, locked),
            )
            self._conn.commit()

    def clear(self) -> None:
        self.save(PinAttemptState())

"""Opening the database, and the conversation store built on it.

Both are boot concerns rather than plain composition: the corruption handling has a policy in it -
a database that fails its integrity check is renamed aside and a fresh one is created, so Nox
starts and the old file is still there to look at - and the turn store carries the retention rule.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nox.core.logging import get_logger
from nox.data.db import Database
from nox.data.repos import TurnRepository

log = get_logger(__name__)

__all__ = ["DbTurnStore", "open_database"]


def open_database(path: Path) -> Database:
    """Open `path`, migrate it, and rename it aside if it is corrupt.

    Synchronous by design and slow on a cold disk - migrations plus the vector extension - so the
    caller runs it in a worker thread.
    """
    db = Database(path)
    if not db.integrity_check():
        db.close()
        corrupt = path.with_name(f"{path.name}.corrupt-{datetime.now(UTC):%Y%m%d%H%M%S}")
        path.rename(corrupt)
        log.error("db.corrupt_renamed", path=str(corrupt))
        db = Database(path)
    applied = db.migrate()
    if applied:
        log.info("db.migrated", migrations=applied)
    return db


class DbTurnStore:
    """The orchestrator's conversation store, on the SQLite repositories.

    Text only. How long a turn is kept comes from the privacy retention setting; `None` means the
    turn has no expiry of its own and is covered by the general retention job.
    """

    def __init__(self, turns: TurnRepository, retention_days: int | None) -> None:
        self._turns = turns
        self._retention_days = retention_days

    async def record(
        self, session_id: str, role: str, text: str, *, provider: str = "", latency_ms: int = 0
    ) -> None:
        retain_until = (
            datetime.now(UTC) + timedelta(days=self._retention_days)
            if self._retention_days
            else None
        )
        await asyncio.to_thread(
            self._turns.add,
            session_id,
            role,
            text,
            provider=provider,
            latency_ms=latency_ms or None,
            retain_until=retain_until,
        )

    async def recent(self, session_id: str, limit: int) -> list[tuple[str, str]]:
        rows = await asyncio.to_thread(self._turns.list_for_session, session_id, 1000)
        return [(row.role, row.text) for row in rows[-limit:]]

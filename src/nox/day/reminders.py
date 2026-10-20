"""Reminders the user set in conversation, and the loop that speaks them when they are due.

"Erinner mich morgen um 9 an den Zahnarzt" is the simplest promise an assistant can make and
the one that is noticed most when it is broken. So the store is a table, not a task: a reminder
survives a restart, is fired at most once (`fired_at` is set *before* Nox speaks), and stays
visible on the dashboard for the rest of the day after it fired. A callout that fails outright
(nothing was published) reopens the reminder for the next poll, up to `MAX_FIRE_ATTEMPTS` times.

Every timestamp is stored as ISO-8601 UTC and handed back timezone-aware; callers format it in
the local zone.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from nox.core.logging import get_logger
from nox.data.db import Database
from nox.data.repos import Row, written_row

log = get_logger(__name__)

__all__ = [
    "MAX_AHEAD",
    "MAX_FIRE_ATTEMPTS",
    "MAX_TEXT_CHARS",
    "ReminderRepository",
    "ReminderRow",
    "ReminderScheduler",
]

#: Longest reminder text; anything longer is a note, and the vault is the place for notes.
MAX_TEXT_CHARS = 300

#: Furthest ahead a reminder may be set; a year is plenty and catches "2062" typos.
MAX_AHEAD = timedelta(days=366)

#: How often a reminder whose callout raised is tried again (one try per poll) before it is given
#: up and left marked fired. A failed callout is one where nothing reached the user at all.
MAX_FIRE_ATTEMPTS = 3


class ReminderRow(Row):
    id: str
    text: str
    due_at: datetime
    created_at: datetime
    source: str = ""
    fired_at: datetime | None = None
    cancelled_at: datetime | None = None

    @property
    def open(self) -> bool:
        return self.fired_at is None and self.cancelled_at is None


def _utc_iso(value: datetime) -> str:
    """UTC ISO text for the database; a naive datetime is read as local time."""
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _now() -> datetime:
    return datetime.now(UTC)


class ReminderRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(self, *, text: str, due_at: datetime, source: str = "") -> ReminderRow:
        cleaned = " ".join(text.split())[:MAX_TEXT_CHARS]
        if not cleaned:
            raise ValueError("a reminder needs a text")
        rid = uuid.uuid4().hex
        self._db.execute(
            "INSERT INTO reminders (id, text, due_at, created_at, source) VALUES (?, ?, ?, ?, ?)",
            (rid, cleaned, _utc_iso(due_at), _utc_iso(_now()), source),
        )
        return written_row(self.get(rid), "reminders", rid)

    def get(self, reminder_id: str) -> ReminderRow | None:
        row = self._db.fetch_one("SELECT * FROM reminders WHERE id = ?", (reminder_id,))
        return None if row is None else ReminderRow(**dict(row))

    def list_open(self, *, limit: int = 50) -> list[ReminderRow]:
        """Not yet fired and not cancelled, soonest first."""
        rows = self._db.fetch_all(
            "SELECT * FROM reminders WHERE fired_at IS NULL AND cancelled_at IS NULL "
            "ORDER BY due_at LIMIT ?",
            (limit,),
        )
        return [ReminderRow(**dict(r)) for r in rows]

    def list_due(self, now: datetime | None = None) -> list[ReminderRow]:
        rows = self._db.fetch_all(
            "SELECT * FROM reminders WHERE fired_at IS NULL AND cancelled_at IS NULL "
            "AND due_at <= ? ORDER BY due_at",
            (_utc_iso(now or _now()),),
        )
        return [ReminderRow(**dict(r)) for r in rows]

    def list_between(self, start: datetime, end: datetime) -> list[ReminderRow]:
        """Everything due in `[start, end)` that was not cancelled - fired ones included, so the
        dashboard can show the morning's reminder in the afternoon."""
        rows = self._db.fetch_all(
            "SELECT * FROM reminders WHERE cancelled_at IS NULL AND due_at >= ? AND due_at < ? "
            "ORDER BY due_at",
            (_utc_iso(start), _utc_iso(end)),
        )
        return [ReminderRow(**dict(r)) for r in rows]

    def mark_fired(self, reminder_id: str, now: datetime | None = None) -> bool:
        cur = self._db.execute(
            "UPDATE reminders SET fired_at = ? WHERE id = ? AND fired_at IS NULL "
            "AND cancelled_at IS NULL",
            (_utc_iso(now or _now()), reminder_id),
        )
        return cur.rowcount == 1

    def reopen(self, reminder_id: str) -> bool:
        """Undo `mark_fired` for a callout that never reached the user (unless cancelled since)."""
        cur = self._db.execute(
            "UPDATE reminders SET fired_at = NULL WHERE id = ? AND fired_at IS NOT NULL "
            "AND cancelled_at IS NULL",
            (reminder_id,),
        )
        return cur.rowcount == 1

    def cancel(self, reminder_id: str, now: datetime | None = None) -> bool:
        cur = self._db.execute(
            "UPDATE reminders SET cancelled_at = ? WHERE id = ? AND fired_at IS NULL "
            "AND cancelled_at IS NULL",
            (_utc_iso(now or _now()), reminder_id),
        )
        return cur.rowcount == 1

    def purge(self, *, older_than: timedelta = timedelta(days=30)) -> int:
        """Forget fired or cancelled reminders older than `older_than`."""
        cur = self._db.execute(
            "DELETE FROM reminders WHERE (fired_at IS NOT NULL OR cancelled_at IS NOT NULL) "
            "AND due_at < ?",
            (_utc_iso(_now() - older_than),),
        )
        return int(cur.rowcount)


Fire = Callable[[ReminderRow], Awaitable[None]]


class ReminderScheduler:
    """Polls for due reminders and hands each one to `fire` exactly once.

    A poll every `poll_s` seconds rather than one timer per reminder: it survives a clock jump,
    a laptop that slept through the due time, and a reminder set from another process - and a
    reminder that is twenty seconds late is still a reminder.
    """

    def __init__(
        self,
        repo: ReminderRepository,
        *,
        fire: Fire,
        clock: Callable[[], datetime] = _now,
        poll_s: float = 20.0,
        deliverable: Callable[[], bool] = lambda: True,
    ) -> None:
        self._repo = repo
        self._fire = fire
        self._clock = clock
        self._deliverable = deliverable
        self._poll_s = poll_s
        self._task: asyncio.Task[None] | None = None
        self._attempts: dict[str, int] = {}
        #: Ticks that raised since start (for the health check), and the last error's type.
        self.tick_failures = 0
        self.last_tick_error = ""
        #: Whether the most recent tick raised.
        self.failing = False

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="nox-reminders")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def tick(self) -> list[str]:
        """Fire what is due now. Returns the ids fired, for tests and logs."""
        fired: list[str] = []
        if not self._deliverable():
            # Nothing would be heard or seen: leave the reminders open rather than mark them
            # fired and lose them. They go out as soon as a way to deliver them is back.
            return fired
        for reminder in self._repo.list_due(self._clock()):
            # Marked before speaking: a reminder repeated after a crash mid-sentence would be
            # worse than one that was cut off, and the record says it was attempted.
            if not self._repo.mark_fired(reminder.id, self._clock()):
                continue
            try:
                await self._fire(reminder)
            except Exception as exc:  # noqa: BLE001 - one failed callout never stops the others
                attempts = self._attempts.get(reminder.id, 0) + 1
                if attempts < MAX_FIRE_ATTEMPTS and self._repo.reopen(reminder.id):
                    self._attempts[reminder.id] = attempts
                    log.warning(
                        "reminders.fire_failed_retrying",
                        id=reminder.id,
                        attempt=attempts,
                        error=type(exc).__name__,
                    )
                else:
                    self._attempts.pop(reminder.id, None)
                    log.error("reminders.fire_failed", id=reminder.id, error=type(exc).__name__)
                continue
            self._attempts.pop(reminder.id, None)
            fired.append(reminder.id)
        if fired:
            log.info("reminders.fired", count=len(fired))
        return fired

    async def run_once(self) -> None:
        """One poll of the loop: a tick that raises is logged and counted, never fatal."""
        try:
            await self.tick()
        except Exception as exc:  # noqa: BLE001 - the loop must survive; the health check reports it
            self.tick_failures += 1
            self.last_tick_error = type(exc).__name__
            self.failing = True
            log.error("reminders.tick_failed", error=self.last_tick_error, total=self.tick_failures)
            return
        self.failing = False

    async def _loop(self) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(self._poll_s)

"""Opening the database, and the conversation store built on it.

Both are boot concerns rather than plain composition: the corruption handling has a policy in it -
a database that cannot be opened or fails its integrity check is renamed aside (with its WAL and
shared-memory files, so the most recent commits are still there to look at) and a fresh one is
created, so Nox starts and says loudly what happened - and every migration is preceded by a backup
into `paths.backups_dir`, of which the newest `DB_BACKUPS_KEEP` are kept. The turn store carries the
retention rule.
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nox.core.logging import get_logger
from nox.data.db import Database
from nox.data.repos import TurnRepository
from nox.ipc.protocol import ChatHistoryTurn

log = get_logger(__name__)

__all__ = [
    "DB_BACKUPS_KEEP",
    "DB_BACKUP_MAX_AGE_DAYS",
    "DatabaseRecovery",
    "DbTurnStore",
    "OpenedDatabase",
    "open_database",
    "purge_old_backups",
]

#: How many pre-migration backups `paths.backups_dir` keeps; older ones are deleted.
DB_BACKUPS_KEEP = 5

#: A backup older than this is deleted by the retention job: it holds rows that retention has
#: long since removed from the live database, and it is too old to roll an upgrade back to.
DB_BACKUP_MAX_AGE_DAYS = 30

#: The files SQLite keeps next to a WAL database. They belong to the damaged file and move with it.
_SIDE_FILES = ("-wal", "-shm")

#: Suffix of the copy `_preserve_wal` takes of a leftover WAL while the database is being opened.
_PRESERVED_WAL = "-wal.preserved"


@dataclass(frozen=True, slots=True)
class DatabaseRecovery:
    """A damaged database that was set aside at boot, and why."""

    moved_to: Path
    reason: str

    def describe(self) -> str:
        """For health and the audit log: the file name only, never the full path."""
        return (
            "the database was damaged and has been set aside as "
            f"{self.moved_to.name}; Nox started with an empty one ({self.reason})"
        )


@dataclass(frozen=True, slots=True)
class OpenedDatabase:
    """What `open_database` did: the database, plus the recovery and backup it performed."""

    db: Database
    recovery: DatabaseRecovery | None = None
    backup: Path | None = None
    backup_error: str = ""


def open_database(
    path: Path,
    *,
    backups_dir: Path | None = None,
    keep_backups: int = DB_BACKUPS_KEEP,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> OpenedDatabase:
    """Open `path`, set it aside if it is damaged, back it up, and migrate it.

    Synchronous by design and slow on a cold disk - integrity check, backup and migrations - so the
    caller runs it in a worker thread.
    """
    preserved = _preserve_wal(path)
    db, reason = _open_checked(path)
    recovery: DatabaseRecovery | None = None
    if db is None:
        moved_to = _move_aside(path, now(), preserved)
        recovery = DatabaseRecovery(moved_to=moved_to, reason=reason)
        log.critical("db.corrupt_renamed", moved_to=moved_to.name, reason=reason)
        db = Database(path)
    elif preserved is not None:
        preserved.unlink(missing_ok=True)
    backup: Path | None = None
    backup_error = ""
    pending = db.pending_migrations()
    if pending and backups_dir is not None and db.applied_migrations():
        try:
            backup = _backup(db, backups_dir, pending[0], now(), keep_backups)
        except (OSError, sqlite3.Error) as exc:
            # Migrating without a backup is still better than a core that cannot start; the
            # failure is logged loudly and reported by the database health check.
            backup_error = f"{type(exc).__name__}: {exc}"
            log.error("db.backup_failed", error=backup_error, note="migrating without a backup")
    applied = db.migrate()
    if applied:
        log.info("db.migrated", migrations=applied, backup=backup.name if backup else None)
    return OpenedDatabase(db=db, recovery=recovery, backup=backup, backup_error=backup_error)


def _open_checked(path: Path) -> tuple[Database | None, str]:
    """The opened database, or None and the reason when it cannot be opened or is corrupt."""
    try:
        db = Database(path)
    except sqlite3.OperationalError:
        # Locked, unreadable, a missing directory: the file may be perfectly fine, and renaming
        # a healthy database aside would lose it for no reason. Boot fails and says why.
        raise
    except sqlite3.DatabaseError as exc:  # "file is not a database", "disk image is malformed"
        return None, f"cannot open: {exc}"
    if db.integrity_check():
        return db, ""
    db.close()
    return None, "integrity check failed"


def _preserve_wal(path: Path) -> Path | None:
    """Copy a leftover WAL before SQLite gets to see it.

    A WAL is only still there after an unclean shutdown, and it holds the most recent commits.
    Closing a connection that failed on a damaged main file deletes it, so the copy is what keeps
    those commits for whoever looks at the damaged file later. A healthy open drops the copy again.
    """
    wal = path.with_name(path.name + "-wal")
    if not wal.is_file():
        return None
    copy = path.with_name(path.name + _PRESERVED_WAL)
    try:
        shutil.copyfile(wal, copy)
    except OSError as exc:
        log.warning("db.wal_preserve_failed", error=str(exc))
        return None
    return copy


def _move_aside(path: Path, when: datetime, preserved_wal: Path | None) -> Path:
    """Rename the damaged file and its WAL/shm files to `<name>.corrupt-<timestamp>[-wal]`."""
    target = path.with_name(f"{path.name}.corrupt-{when:%Y%m%d%H%M%S}")
    suffix = 1
    while target.exists():
        target = path.with_name(f"{path.name}.corrupt-{when:%Y%m%d%H%M%S}-{suffix}")
        suffix += 1
    path.rename(target)
    for side in _SIDE_FILES:
        companion = path.with_name(path.name + side)
        if companion.exists():
            companion.rename(target.with_name(target.name + side))
    if preserved_wal is not None and preserved_wal.exists():
        preserved_wal.replace(target.with_name(target.name + "-wal"))
    return target


def purge_old_backups(backups_dir: Path, now: datetime, *, max_age_days: int) -> int:
    """Delete pre-migration backups written more than `max_age_days` ago. Returns how many."""
    cutoff = (now - timedelta(days=max_age_days)).timestamp()
    removed = 0
    for backup in backups_dir.glob("*-before-*.db") if backups_dir.is_dir() else ():
        if backup.stat().st_mtime < cutoff:
            backup.unlink()
            removed += 1
    return removed


def _backup(db: Database, backups_dir: Path, before: str, when: datetime, keep: int) -> Path:
    """Write `<stem>-<timestamp>-before-<migration>.db` and prune all but the newest `keep`."""
    stem = db.path.stem
    target = backups_dir / f"{stem}-{when:%Y%m%d%H%M%S}-before-{before}.db"
    db.backup_to(target)
    log.info("db.backup_written", backup=target.name)
    backups = sorted(backups_dir.glob(f"{stem}-*-before-*.db"))
    for old in backups[: max(0, len(backups) - keep)]:
        try:
            old.unlink()
        except OSError as exc:
            log.warning("db.backup_prune_failed", backup=old.name, error=str(exc))
    return target


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

    async def history(
        self, limit: int, *, before: int | None = None
    ) -> tuple[list[ChatHistoryTurn], bool]:
        """`chat.history`: persisted turns across sessions, oldest first, plus "older exist"."""
        rows, has_more = await asyncio.to_thread(self._turns.list_history, limit, before_id=before)
        turns = [
            ChatHistoryTurn(
                id=row.id,
                session_id=row.session_id,
                ts=row.ts.isoformat(),
                role=row.role,
                text=row.text,
                provider=row.provider,
            )
            for row in rows
        ]
        return turns, has_more

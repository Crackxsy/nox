"""The retention job: every table with a retention rule is purged on a schedule.

`docs/PRIVACY.md` promises how long each kind of data is kept. The repositories could always delete
expired rows; nothing ever asked them to, so transcripts, chat and health history were kept
forever. `RetentionService` runs every rule in `retention_rules()` - one per table, each naming the
configuration key it follows - a minute after boot and then every six hours, audits what it
removed, and reports the last successful run in health as `retention`.

What is deliberately not here: the audit log (append-only and hash-chained; deleting from it is
tampering by design), stream replays (never deleted by Nox, only on the user's request) and log
files (rotated by the logging setup itself). `docs/PRIVACY.md` lists those too.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from nox.core.boot.persistence import DB_BACKUP_MAX_AGE_DAYS, purge_old_backups
from nox.core.config import NoxConfig
from nox.core.events import HealthStatus
from nox.core.logging import get_logger
from nox.data.db import Database
from nox.data.repos import (
    HealthHistoryRepository,
    MemoryItemRepository,
    NotificationRepository,
    StateCheckpointRepository,
    TemporaryGrantRepository,
    TurnRepository,
)
from nox.data.rl_repos import RlEventRepository, RlMatchRepository
from nox.data.rl_vision_repos import RlVisionFrameRepository
from nox.data.stream_repos import (
    ChatEventRepository,
    ViewerMemoryRepository,
    ViewerRepository,
)
from nox.memory.retention import AuditSink, RetentionJob
from nox.remote.repo import RemoteRepository

log = get_logger(__name__)

__all__ = [
    "RETENTION_FIRST_RUN_DELAY_S",
    "RETENTION_INTERVAL_S",
    "RetentionRule",
    "RetentionRun",
    "RetentionService",
    "retention_rules",
]

#: The first run waits this long after boot, out of the way of the busiest minute.
RETENTION_FIRST_RUN_DELAY_S = 60.0

#: Then it runs this often. Expiry is measured in days; four runs a day keep every promise within
#: hours of its deadline without the job ever being noticeable.
RETENTION_INTERVAL_S = 6 * 3600.0

Now = Callable[[], datetime]
Purge = Callable[[datetime], "int | Awaitable[int]"]


class _MemoryRetention(Protocol):
    """`nox.memory.retention.RetentionJob`: memory items need their embeddings removed too."""

    async def run(self, now: datetime | None = None) -> Any: ...


@dataclass(frozen=True, slots=True)
class RetentionRule:
    """One table: what decides how long its rows live, and how to remove the expired ones."""

    table: str
    config_key: str
    purge: Purge


@dataclass(frozen=True, slots=True)
class RetentionRun:
    """The outcome of one run: rows removed per table, and the tables that failed."""

    at: datetime
    removed: dict[str, int] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.removed.values())


def retention_rules(
    db: Database,
    config: NoxConfig,
    *,
    memory: Callable[[], _MemoryRetention | None] = lambda: None,
    audit: AuditSink | None = None,
) -> list[RetentionRule]:
    """Every retention-bound table, in the order `docs/PRIVACY.md` lists them."""
    privacy = config.privacy.retention
    turns = TurnRepository(db)
    notifications = NotificationRepository(db)
    matches = RlMatchRepository(db)

    def purge_turns(now: datetime) -> int:
        removed = turns.purge_expired(now)
        if privacy.raw_transcripts_days > 0:
            removed += turns.purge_older_than(privacy.raw_transcripts_days, now)
        return removed

    fallback = RetentionJob(
        MemoryItemRepository(db),
        db,
        audit=audit,
        note_version_retention_days=config.memory.retention_note_version_days,
    )

    async def purge_memory(now: datetime) -> int:
        # The memory extension's job also removes each item's embedding; without the extension
        # there are no embeddings to remove, and the rows still expire.
        job = memory() or fallback
        report = await job.run(now)
        return int(report.memory_items_purged) + int(report.note_versions_purged)

    return [
        RetentionRule("turns", "privacy.retention.raw_transcripts_days", purge_turns),
        RetentionRule(
            "chat_events",
            "stream.chat.retain_raw_text_days",
            lambda now: ChatEventRepository(db).purge_expired(now),
        ),
        RetentionRule(
            "viewer_memory",
            "privacy.retention.viewer_data_inactive_months",
            lambda now: ViewerMemoryRepository(db).purge_expired(now),
        ),
        RetentionRule(
            "viewers",
            "privacy.retention.viewer_data_inactive_months",
            lambda now: (
                ViewerRepository(db).purge_expired(now)
                + ViewerRepository(db).purge_inactive(privacy.viewer_data_inactive_months, now)
            ),
        ),
        RetentionRule(
            "health_history",
            "privacy.retention.metrics_days",
            lambda now: HealthHistoryRepository(db).purge_older_than(privacy.metrics_days, now),
        ),
        RetentionRule(
            "proactive_notifications",
            "proactive.notifications_retention_days",
            lambda now: notifications.purge_expired(
                now=now, retention_days=config.proactive.notifications_retention_days
            ),
        ),
        RetentionRule(
            "memory_items, vault_note_versions",
            "memory.retention_note_version_days",
            purge_memory,
        ),
        RetentionRule(
            "rl_events",
            "rl.retention.events_days",
            lambda now: RlEventRepository(db).purge_expired(now=now),
        ),
        RetentionRule(
            "rl_matches",
            "rl.retention.matches_days",
            lambda now: matches.purge_older_than(config.rl.retention.matches_days, now=now),
        ),
        RetentionRule(
            "rl_vision_frames",
            "rl.vision.detections_retain_hours",
            lambda now: RlVisionFrameRepository(db).purge_expired(now=now),
        ),
        RetentionRule(
            "temporary_grants",
            "(each grant's own expiry)",
            lambda now: TemporaryGrantRepository(db).purge(now),
        ),
        RetentionRule(
            "remote_pairings",
            "remote.pair_code_ttl_s",
            lambda now: RemoteRepository(db).purge_expired_pairings(now),
        ),
        RetentionRule(
            "database backups",
            f"({DB_BACKUP_MAX_AGE_DAYS} days)",
            lambda now: purge_old_backups(
                Path(config.paths.backups_dir), now, max_age_days=DB_BACKUP_MAX_AGE_DAYS
            ),
        ),
        RetentionRule(
            "state_checkpoints",
            f"(newest {StateCheckpointRepository.KEEP} kept)",
            lambda _now: StateCheckpointRepository(db).prune(),
        ),
    ]


class RetentionService:
    """Runs the rules on a schedule and remembers how the last run went, for health."""

    def __init__(
        self,
        rules: list[RetentionRule],
        *,
        audit: AuditSink | None = None,
        now: Now = lambda: datetime.now(UTC),
        first_run_delay_s: float = RETENTION_FIRST_RUN_DELAY_S,
        interval_s: float = RETENTION_INTERVAL_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._rules = rules
        self._audit = audit
        self._now = now
        self._first_run_delay_s = first_run_delay_s
        self._interval_s = interval_s
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None
        self.last_run: RetentionRun | None = None
        self.last_success: datetime | None = None

    async def run_once(self) -> RetentionRun:
        """Apply every rule once. A failing table is recorded and the others still run."""
        now = self._now()
        run = RetentionRun(at=now)
        for rule in self._rules:
            try:
                run.removed[rule.table] = await self._apply(rule, now)
            except Exception as exc:  # noqa: BLE001 - one table must not keep the others' promises
                run.failed[rule.table] = f"{type(exc).__name__}: {exc}"
                log.error("retention.table_failed", table=rule.table, error=run.failed[rule.table])
        self.last_run = run
        if not run.failed:
            self.last_success = now
        removed = {table: n for table, n in run.removed.items() if n}
        log.info("retention.ran", removed=removed, failed=sorted(run.failed))
        self._audit_run(removed, run)
        return run

    @staticmethod
    async def _apply(rule: RetentionRule, now: datetime) -> int:
        if inspect.iscoroutinefunction(rule.purge):
            return int(await rule.purge(now))
        result = await asyncio.to_thread(rule.purge, now)
        if inspect.isawaitable(result):
            result = await result
        return int(result)

    def _audit_run(self, removed: dict[str, int], run: RetentionRun) -> None:
        if self._audit is None or not (removed or run.failed):
            return
        details = {table: str(count) for table, count in removed.items()}
        details.update({f"failed:{table}": "error" for table in run.failed})
        try:
            self._audit.append(
                actor="system",
                tool="retention",
                action="retention.purge",
                target=f"{run.total} rows",
                decision="allow",
                result="partial" if run.failed else "ok",
                details=details,
            )
        except Exception as exc:  # noqa: BLE001 - the purge happened; the log line says so too
            log.error("retention.audit_failed", error=f"{type(exc).__name__}: {exc}")

    # ---- schedule ----------------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="nox-retention")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _loop(self) -> None:
        await self._sleep(self._first_run_delay_s)
        while True:
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 - the next run tries again; health says so
                log.error("retention.run_failed", error=f"{type(exc).__name__}: {exc}")
            await self._sleep(self._interval_s)

    # ---- health ------------------------------------------------------------------------------

    async def health(self) -> tuple[HealthStatus, str]:
        run = self.last_run
        if run is None:
            return (
                HealthStatus.LIMITED,
                f"not run yet; the first run is {self._first_run_delay_s / 60:g} min after start",
            )
        if run.failed:
            last_ok = (
                f"last complete run {self.last_success:%Y-%m-%d %H:%M} UTC"
                if self.last_success is not None
                else "no complete run yet"
            )
            return (
                HealthStatus.LIMITED,
                f"failed for {', '.join(sorted(run.failed))} at {run.at:%Y-%m-%d %H:%M} UTC; "
                f"{last_ok}",
            )
        return (
            HealthStatus.AVAILABLE,
            f"last run {run.at:%Y-%m-%d %H:%M} UTC, {run.total} expired rows removed",
        )

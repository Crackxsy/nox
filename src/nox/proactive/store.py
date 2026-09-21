"""notification store, now backed by `proactive_notifications` (migration `0010_notifications.sql`):
notifications survive a restart, and `dismiss` persists.

`db=None` keeps the original in-memory-ring-buffer behaviour (a bounded `deque`) for callers that
construct a store without a database - every existing unit test that builds a bare
`NotificationStore`/`ProactiveService` keeps working unchanged.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime

from nox.data.db import Database
from nox.data.repos import DEFAULT_NOTIFICATIONS_RETENTION_DAYS, NotificationRepository
from nox.proactive.models import NotificationRecord

#: Re-exported so callers only need `nox.proactive.store` for the default. See
#: `NotificationRepository`'s docstring (`nox.data.repos`) for why this stays a module-level
#: constant rather than a `ProactiveConfig` field in this pass - report flags the field for the
#: config owner to add as `proactive.notifications_retention_days` (default 30).
DEFAULT_RETENTION_DAYS = DEFAULT_NOTIFICATIONS_RETENTION_DAYS


def _now() -> datetime:
    return datetime.now(UTC)


class NotificationStore:
    def __init__(self, *, limit: int = 200, db: Database | None = None) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        self._limit = limit
        self._repo = NotificationRepository(db) if db is not None else None
        # Always kept (even when `_repo` is set) as a zero-cost fallback path; only actually read
        # from when `_repo` is None.
        self._records: deque[NotificationRecord] = deque(maxlen=limit)

    def add(self, record: NotificationRecord) -> None:
        if self._repo is not None:
            self._repo.add(
                id=record.id,
                created_at=record.created_at,
                priority=record.priority,
                kind=record.kind,
                body=record.text,
                source=record.source,
                channel=record.channel,
                spoken=record.spoken,
                announced=record.announced,
                suppressed_reason=record.suppressed_reason,
                dismissed_at=record.dismissed_at,
                expires_at=record.expires_at,
            )
            return
        self._records.append(record)

    def list_recent(self, limit: int = 50) -> list[NotificationRecord]:
        if self._repo is not None:
            rows = self._repo.list_recent(limit)
            return [
                NotificationRecord(
                    id=r.id,
                    created_at=r.created_at,
                    kind=r.kind,
                    priority=r.priority,
                    text=r.body,
                    channel=r.channel,
                    spoken=r.spoken,
                    announced=r.announced,
                    suppressed_reason=r.suppressed_reason,
                    source=r.source,
                    dismissed_at=r.dismissed_at,
                    expires_at=r.expires_at,
                )
                for r in rows
            ]
        items = list(self._records)[-limit:]
        items.reverse()  # newest first, matching HealthHistoryRepository.list_recent's convention
        return items

    def dismiss(self, notification_id: str, *, dismissed_at: datetime | None = None) -> bool:
        """`proactive.notification.dismiss`. Returns `False` for an unknown id or one
        already dismissed (idempotent, matches `NotificationRepository.dismiss`)."""
        if self._repo is not None:
            return self._repo.dismiss(notification_id, dismissed_at=dismissed_at)
        for i, record in enumerate(self._records):
            if record.id == notification_id:
                if record.dismissed_at is not None:
                    return False
                self._records[i] = record.model_copy(
                    update={"dismissed_at": dismissed_at or _now()}
                )
                return True
        return False

    def purge_expired(
        self, *, now: datetime | None = None, retention_days: int = DEFAULT_RETENTION_DAYS
    ) -> int:
        """Nightly-retention-job hook (Data Model convention, mirrors every other
        `*Repository.purge_expired`). A no-op for the in-memory fallback: the bounded `deque`
        already self-prunes by `limit`."""
        if self._repo is not None:
            return self._repo.purge_expired(now=now, retention_days=retention_days)
        return 0

    def __len__(self) -> int:
        if self._repo is not None:
            return len(self._repo.list_recent(self._limit))
        return len(self._records)

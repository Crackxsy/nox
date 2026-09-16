"""ST-19-08 notification store: an in-memory ring buffer of `NotificationRecord`s for
`proactive.status.read`. Deliberately not a DB table in this pass (`ProactiveConfig` docstring: no
migration in this change) - history does not survive a restart yet; a follow-up story can promote
this to a `Row`/`*Repository` (Data Model convention) without changing this class's public shape.
"""

from __future__ import annotations

from collections import deque

from nox.proactive.models import NotificationRecord


class NotificationStore:
    def __init__(self, *, limit: int = 200) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        self._limit = limit
        self._records: deque[NotificationRecord] = deque(maxlen=limit)

    def add(self, record: NotificationRecord) -> None:
        self._records.append(record)

    def list_recent(self, limit: int = 50) -> list[NotificationRecord]:
        items = list(self._records)[-limit:]
        items.reverse()  # newest first, matching HealthHistoryRepository.list_recent's convention
        return items

    def __len__(self) -> int:
        return len(self._records)

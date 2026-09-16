"""Daily focus (`pm.focus.today`): composes the open stories worth surfacing today, ranked by
status then priority then id - a deliberately simple stand-in for ST-13-06's full dependency-aware
priority engine (out of this slice's scope; see this story's report Open Points).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from nox.core.events import E, Event, EventBus
from nox.pm.index import PmIndex
from nox.pm.models import WorkItem

_OPEN_STATUSES = ("in_progress", "todo", "review")
_STATUS_RANK = {"in_progress": 0, "todo": 1, "review": 2}
_UNRANKED_PRIORITY = 9


def _priority_rank(priority: str | None) -> int:
    if not priority:
        return _UNRANKED_PRIORITY
    digits = priority.upper().removeprefix("P")
    return int(digits) if digits.isdigit() else _UNRANKED_PRIORITY


def _reason(item: WorkItem) -> str:
    bits: list[str] = []
    if item.status == "in_progress":
        bits.append("already in progress")
    if item.priority:
        bits.append(f"priority {item.priority}")
    bits.append(f"status {item.status}")
    return ", ".join(bits)


class FocusEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    kind: str
    title: str
    status: str
    priority: str | None
    reason: str


def compute_focus(index: PmIndex, *, limit: int = 5) -> list[FocusEntry]:
    items = index.list_items(kind="story", statuses=_OPEN_STATUSES)
    ranked = sorted(
        items,
        key=lambda i: (
            _STATUS_RANK.get(i.status, len(_STATUS_RANK)),
            _priority_rank(i.priority),
            i.id,
        ),
    )
    return [
        FocusEntry(
            id=i.id,
            kind=i.kind,
            title=i.title,
            status=i.status,
            priority=i.priority,
            reason=_reason(i),
        )
        for i in ranked[:limit]
    ]


class FocusService:
    """Wraps `compute_focus` with change detection so `pm.focus_changed` fires only when the
    ranked id list actually changes, not on every vault reindex pass."""

    def __init__(self, index: PmIndex, bus: EventBus, *, limit: int = 5) -> None:
        self._index = index
        self._bus = bus
        self._limit = limit
        self._last_ids: tuple[str, ...] = ()

    def today(self) -> list[FocusEntry]:
        return compute_focus(self._index, limit=self._limit)

    async def recompute_and_publish(self) -> list[FocusEntry]:
        entries = self.today()
        ids = tuple(e.id for e in entries)
        if ids != self._last_ids:
            self._last_ids = ids
            await self._bus.publish(
                Event(
                    name=E.PM_FOCUS_CHANGED,
                    payload={"items": [e.model_dump(mode="json") for e in entries]},
                )
            )
        return entries

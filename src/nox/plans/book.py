"""`PlanBook`: where proposals wait, and how an approved one reaches the queue.

Two kinds of storage on purpose, and the difference is not an accident of implementation:

* A **proposal** is part of a conversation. It lives in memory, a bounded number of them, and a
  restart forgets it. Persisting proposals would mean a machine that boots with a list of things a
  language model once suggested and nobody agreed to, which is clutter at best.
* An **approved** plan is a task row: payload and progress in the database, resumed after a crash,
  paused while a game runs. Approval is the moment a suggestion becomes work.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from nox.core.logging import get_logger
from nox.data.repos import TaskRow, TaskStatus
from nox.plans.model import Plan
from nox.plans.runner import KIND

log = get_logger(__name__)

__all__ = ["PlanBook"]

#: Proposals kept before the oldest is dropped. Generous for a conversation, bounded so a model in
#: a loop cannot grow the process.
MAX_PROPOSALS = 20

LIVE_STATUSES = (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.DONE, TaskStatus.FAILED)


class QueueLike(Protocol):
    async def submit(
        self, kind: str, payload: dict[str, Any] | None = None, *, priority: int = 0
    ) -> TaskRow: ...


class TasksLike(Protocol):
    def get(self, task_id: str) -> TaskRow | None: ...
    def list_by_status(self, *statuses: TaskStatus, limit: int = 100) -> list[TaskRow]: ...


@dataclass(slots=True)
class PlanBook:
    queue: QueueLike
    tasks: TasksLike
    limit: int = MAX_PROPOSALS
    _proposed: dict[str, Plan] = field(default_factory=dict)

    # ---- proposals ---------------------------------------------------------------------------

    def propose(self, plan: Plan) -> None:
        self._proposed[plan.id] = plan
        while len(self._proposed) > self.limit:
            dropped = next(iter(self._proposed))
            del self._proposed[dropped]
            log.info("plans.proposal_dropped", plan=dropped, reason="too many waiting")

    def proposed(self, plan_id: str) -> Plan | None:
        return self._proposed.get(plan_id)

    def waiting(self) -> list[Plan]:
        return list(self._proposed.values())

    def forget(self, plan_id: str) -> bool:
        return self._proposed.pop(plan_id, None) is not None

    # ---- approval ----------------------------------------------------------------------------

    async def start(self, plan_id: str) -> TaskRow:
        """Hand a proposed plan to the queue. Raises `KeyError` when there is no such proposal.

        The proposal is removed only after the row exists, so a failing queue leaves the plan where
        the user can find it instead of dropping it between the two.
        """
        plan = self._proposed.get(plan_id)
        if plan is None:
            raise KeyError(plan_id)
        row = await self.queue.submit(KIND, plan.model_dump())
        del self._proposed[plan_id]
        log.info("plans.started", plan=plan.id, task=row.id, steps=len(plan.steps))
        return row

    # ---- what happened ------------------------------------------------------------------------

    def rows(self) -> list[TaskRow]:
        """Every plan row the task table still holds, newest first."""
        rows = [row for row in self.tasks.list_by_status(*LIVE_STATUSES) if row.kind == KIND]
        return sorted(rows, key=lambda row: row.created_at, reverse=True)

    def progress(self, plan_id: str) -> dict[str, Any] | None:
        """How far one approved plan got, or `None` when no row carries that plan id."""
        for row in self.rows():
            if str(row.payload.get("id")) == plan_id:
                return _progress(row)
        return None

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        return [_progress(row) for row in self.rows()[:limit]]


def _progress(row: TaskRow) -> dict[str, Any]:
    """One plan row as the dashboard and the model both read it."""
    checkpoint = row.checkpoint or {}
    done: Iterable[dict[str, Any]] = checkpoint.get("done") or []
    steps = row.payload.get("steps") or []
    return {
        "id": row.payload.get("id", ""),
        "title": row.payload.get("title", ""),
        "task": row.id,
        "status": row.status.value,
        "error": row.error,
        "steps_total": len(steps),
        "steps_done": len(list(done)),
        "results": list(done),
        "running_step": (checkpoint.get("attempting") or {}).get("step"),
    }

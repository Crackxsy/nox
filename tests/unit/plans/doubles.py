"""Stand-ins shared by the plan tests: a queue that records, a task table in a list."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from nox.data.repos import TaskRow, TaskStatus
from nox.plans.model import Plan

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def plan(plan_id: str = "p1", title: str = "Aufraeumen") -> Plan:
    return Plan.model_validate(
        {"id": plan_id, "title": title, "steps": [{"tool": "time.now"}, {"tool": "vault.read"}]}
    )


class FakeQueue:
    def __init__(self) -> None:
        self.submitted: list[tuple[str, dict[str, Any]]] = []

    async def submit(
        self, kind: str, payload: dict[str, Any] | None = None, *, priority: int = 0
    ) -> TaskRow:
        self.submitted.append((kind, payload or {}))
        return TaskRow(
            id=f"t{len(self.submitted)}",
            kind=kind,
            payload=payload or {},
            status=TaskStatus.PENDING,
            created_at=NOW,
            updated_at=NOW,
        )


class FakeTasks:
    def __init__(self, rows: list[TaskRow] | None = None) -> None:
        self.rows = rows or []

    def get(self, task_id: str) -> TaskRow | None:
        return next((row for row in self.rows if row.id == task_id), None)

    def list_by_status(self, *statuses: TaskStatus, limit: int = 100) -> list[TaskRow]:
        return [row for row in self.rows if row.status in statuses][:limit]

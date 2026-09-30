"""Where proposals wait, and what approving one actually does."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from nox.data.repos import TaskRow, TaskStatus
from nox.plans.book import PlanBook
from nox.plans.runner import KIND
from tests.unit.plans.doubles import NOW, FakeQueue, FakeTasks, plan


def book(rows: list[TaskRow] | None = None, **kw: Any) -> PlanBook:
    return PlanBook(queue=FakeQueue(), tasks=FakeTasks(rows), **kw)


def test_a_proposal_runs_nothing() -> None:
    shelf = book()

    shelf.propose(plan())

    assert [p.id for p in shelf.waiting()] == ["p1"]
    assert shelf.queue.submitted == [], "proposing must never reach the queue"


async def test_approving_hands_the_whole_plan_to_the_queue() -> None:
    shelf = book()
    shelf.propose(plan())

    row = await shelf.start("p1")

    kind, payload = shelf.queue.submitted[0]
    assert kind == KIND
    assert payload["title"] == "Aufraeumen"
    assert len(payload["steps"]) == 2, "the steps have to be in the row, not in memory"
    assert shelf.waiting() == [], "an approved plan is no longer waiting"
    assert row.id == "t1"


async def test_approving_something_that_is_not_waiting_is_an_error() -> None:
    with pytest.raises(KeyError):
        await book().start("nope")


def test_a_discarded_proposal_is_gone() -> None:
    shelf = book()
    shelf.propose(plan())

    assert shelf.forget("p1") is True
    assert shelf.forget("p1") is False


def test_proposals_are_bounded() -> None:
    """A model in a loop must not be able to grow the process one suggestion at a time."""
    shelf = book(limit=2)

    for index in range(4):
        shelf.propose(plan(plan_id=f"p{index}"))

    assert [p.id for p in shelf.waiting()] == ["p2", "p3"]


# ---- what happened ------------------------------------------------------------------------------


def started_row(status: TaskStatus, done: int, attempting: dict[str, Any] | None = None) -> TaskRow:
    return TaskRow(
        id="t1",
        kind=KIND,
        payload=plan().model_dump(),
        checkpoint={
            "done": [{"step": i, "tool": "time.now", "ok": True, "error": ""} for i in range(done)],
            "attempting": attempting,
        },
        status=status,
        created_at=NOW,
        updated_at=NOW,
    )


def test_progress_counts_the_steps_that_finished() -> None:
    shelf = book([started_row(TaskStatus.RUNNING, done=1, attempting={"step": 1})])

    progress = shelf.progress("p1")

    assert progress is not None
    assert (progress["steps_done"], progress["steps_total"]) == (1, 2)
    assert progress["running_step"] == 1
    assert progress["status"] == "running"


def test_a_plan_that_was_never_started_has_no_progress() -> None:
    assert book().progress("p1") is None


def test_the_history_is_newest_first() -> None:
    older = started_row(TaskStatus.DONE, done=2)
    newer = started_row(TaskStatus.FAILED, done=1)
    newer = newer.model_copy(update={"id": "t2", "created_at": NOW + timedelta(minutes=5)})

    entries = book([older, newer]).history()

    assert [entry["task"] for entry in entries] == ["t2", "t1"]


def test_rows_of_other_kinds_are_ignored() -> None:
    """The task table is shared with every other kind of background work."""
    other = started_row(TaskStatus.DONE, done=2).model_copy(update={"kind": "rl_backfill"})

    assert book([other]).history() == []

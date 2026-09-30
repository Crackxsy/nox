"""Running a plan, and the awkward part: coming back from a restart in the middle of one.

The test that carries the design is `test_a_step_that_was_running_is_not_repeated`. A step is a tool
call with side effects, so repeating one after a crash is worse than leaving it undone - and the
only way to know which case you are in is to write the attempt down before making it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel

from nox.data.repos import TaskRow, TaskStatus
from nox.plans.runner import INTERRUPTED, KIND, PlanStepError, plan_handler

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


class Outcome(BaseModel):
    ok: bool = True
    data: dict[str, Any] | None = None
    error: str | None = None


class Executor:
    """Records every call and answers from a per-tool script."""

    def __init__(self, failing: dict[str, str] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.failing = failing or {}

    async def __call__(self, name: str, arguments: dict[str, Any]) -> Outcome:
        self.calls.append((name, arguments))
        if name in self.failing:
            return Outcome(ok=False, error=self.failing[name])
        return Outcome(ok=True, data={"tool": name})


class Checkpoints:
    """The queue's checkpoint callback, keeping every write so the order can be asserted."""

    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    def __call__(self, data: dict[str, Any]) -> None:
        self.writes.append(data)

    @property
    def last(self) -> dict[str, Any]:
        return self.writes[-1]


def plan(*tools: str, stop_on_error: bool = True) -> dict[str, Any]:
    return {
        "id": "p1",
        "title": "Aufraeumen",
        "steps": [{"tool": tool, "arguments": {"n": i}} for i, tool in enumerate(tools)],
        "stop_on_error": stop_on_error,
    }


def row(payload: dict[str, Any], checkpoint: dict[str, Any] | None = None) -> TaskRow:
    return TaskRow(
        id="t1",
        kind=KIND,
        payload=payload,
        checkpoint=checkpoint,
        status=TaskStatus.RUNNING,
        created_at=NOW,
        updated_at=NOW,
    )


async def test_every_step_runs_in_order() -> None:
    executor = Executor()
    marks = Checkpoints()

    await plan_handler(executor)(row(plan("time.now", "vault.read")), marks)

    assert [name for name, _ in executor.calls] == ["time.now", "vault.read"]
    assert [result["step"] for result in marks.last["done"]] == [0, 1]
    assert marks.last["attempting"] is None


async def test_arguments_reach_the_tool_untouched() -> None:
    executor = Executor()

    await plan_handler(executor)(row(plan("time.now")), Checkpoints())

    assert executor.calls == [("time.now", {"n": 0})]


async def test_a_failing_step_stops_the_plan_and_names_itself() -> None:
    executor = Executor(failing={"vault.read": "not allowed here"})
    marks = Checkpoints()

    with pytest.raises(PlanStepError, match="step 1"):
        await plan_handler(executor)(row(plan("time.now", "vault.read", "time.now")), marks)

    assert [name for name, _ in executor.calls] == ["time.now", "vault.read"]
    assert marks.last["done"][1] == {
        "step": 1,
        "tool": "vault.read",
        "ok": False,
        "error": "not allowed here",
    }


async def test_without_stop_on_error_the_rest_still_runs() -> None:
    executor = Executor(failing={"vault.read": "nope"})

    with pytest.raises(PlanStepError, match="these steps did not work"):
        await plan_handler(executor)(
            row(plan("time.now", "vault.read", "time.now", stop_on_error=False)), Checkpoints()
        )

    assert len(executor.calls) == 3, "the steps after a failure were asked for explicitly"


# ---- after a restart ---------------------------------------------------------------------------


async def test_finished_steps_are_not_repeated() -> None:
    """The queue resets RUNNING to PENDING and keeps the checkpoint; this is what that buys."""
    executor = Executor()
    done = [{"step": 0, "tool": "time.now", "ok": True, "error": ""}]

    await plan_handler(executor)(
        row(plan("time.now", "vault.read"), {"done": done, "attempting": None}), Checkpoints()
    )

    assert [name for name, _ in executor.calls] == ["vault.read"]


async def test_a_step_that_was_running_is_not_repeated() -> None:
    """The core died inside step 1. Running it again could start a program a second time."""
    executor = Executor()
    marks = Checkpoints()
    checkpoint = {
        "done": [{"step": 0, "tool": "time.now", "ok": True, "error": ""}],
        "attempting": {"step": 1, "tool": "presets.run_action"},
    }

    with pytest.raises(PlanStepError, match=INTERRUPTED):
        await plan_handler(executor)(row(plan("time.now", "presets.run_action"), checkpoint), marks)

    assert executor.calls == [], "nothing may be called again after an interrupted step"
    assert marks.last["done"][1]["error"] == INTERRUPTED


async def test_the_attempt_is_written_down_before_the_call() -> None:
    """Without this the interrupted case cannot be told from the never-started one."""
    executor = Executor()
    marks = Checkpoints()

    await plan_handler(executor)(row(plan("time.now")), marks)

    assert marks.writes[0]["attempting"] == {"step": 0, "tool": "time.now"}
    assert marks.writes[0]["done"] == []

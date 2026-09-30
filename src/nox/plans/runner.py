"""Running an approved plan on the task queue, and surviving a restart in the middle of one.

The queue already persists the payload and a checkpoint, resets RUNNING tasks to PENDING at startup
and pauses itself while a game is on. So a plan needs none of that - it needs one decision the queue
cannot make for it: what to do about the step that was in flight when the machine went down.

Repeating it would be easy and wrong. A step is a tool call with side effects; starting a program or
writing a file twice is worse than not doing it at all. So the step being attempted is written down
*before* it runs, and a plan that comes back from a restart marks that step as interrupted and stops
there. The user sees a plan that got to step three and why, and can approve the rest - which is a
worse outcome than a perfect resume and a much better one than a silent repeat.

One consequence of running in the background is worth stating rather than discovering: a step
whose tool needs confirmation opens a dialog, and a plan running while the user is away will
sit on that dialog until it times out and then record the step as refused. That is the safe
direction - nothing happens unattended that the profile wanted a person to see - but it does
mean that a plan of confirmable steps is a plan to run while watching.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from nox.core.logging import get_logger
from nox.data.repos import TaskRow
from nox.plans.model import Plan, StepResult

log = get_logger(__name__)

__all__ = ["KIND", "PlanStepError", "plan_handler"]

#: The task kind. Plans share the task table with every other kind of background work.
KIND = "plan"

INTERRUPTED = "interrupted by a restart"


class Outcome(Protocol):
    ok: bool
    data: dict[str, Any] | None
    error: str | None


Call = Callable[[str, dict[str, Any]], Awaitable[Outcome]]
Checkpoint = Callable[[dict[str, Any]], None]


class PlanStepError(Exception):
    """Raised so the queue records the task as failed with the step that broke it."""


def _state(row: TaskRow) -> tuple[list[StepResult], dict[str, Any] | None]:
    """The results already recorded, and the step that was in flight if the core died inside one."""
    checkpoint = row.checkpoint or {}
    done = [StepResult.model_validate(entry) for entry in checkpoint.get("done", [])]
    attempting = checkpoint.get("attempting")
    return done, attempting if isinstance(attempting, dict) else None


def plan_handler(call: Call) -> Callable[[TaskRow, Checkpoint], Awaitable[None]]:
    """A task handler for `KIND`, calling every step through the given executor."""

    async def handle(row: TaskRow, checkpoint: Checkpoint) -> None:
        plan = Plan.model_validate(row.payload)
        done, attempting = _state(row)

        def save(current: dict[str, Any] | None) -> None:
            checkpoint({"done": [result.model_dump() for result in done], "attempting": current})

        if attempting is not None:
            step_index = int(attempting.get("step", len(done)))
            done.append(
                StepResult(
                    step=step_index,
                    tool=str(attempting.get("tool", "")),
                    ok=False,
                    error=INTERRUPTED,
                )
            )
            save(None)
            log.warning("plans.step_interrupted", plan=plan.id, step=step_index)
            if plan.stop_on_error:
                raise PlanStepError(f"{plan.title}: step {step_index} was {INTERRUPTED}")

        for index in range(len(done), len(plan.steps)):
            step = plan.steps[index]
            save({"step": index, "tool": step.tool})
            outcome = await call(step.tool, step.arguments)
            result = StepResult(
                step=index,
                tool=step.tool,
                ok=bool(outcome.ok),
                error=str(outcome.error or ""),
            )
            done.append(result)
            save(None)
            log.info("plans.step", plan=plan.id, step=index, tool=step.tool, ok=result.ok)
            if not result.ok and plan.stop_on_error:
                raise PlanStepError(f"{plan.title}: step {index} ({step.tool}) {result.error}")

        failed = [result for result in done if not result.ok]
        log.info("plans.finished", plan=plan.id, steps=len(done), failed=len(failed))
        if failed and not plan.stop_on_error:
            # Every step ran; some did not work. The task is still a failure, and the message names
            # which steps, because "the plan failed" on its own tells the user nothing.
            names = ", ".join(f"{result.step} ({result.tool})" for result in failed)
            raise PlanStepError(f"{plan.title}: these steps did not work: {names}")

    return handle

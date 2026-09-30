"""The `plans.*` tools: propose, look at, start, follow.

The split between them is the safety property. `plans.propose` writes nothing to the machine and
carries low risk; it produces a numbered list a person can read. `plans.start` carries high risk and
therefore falls to a confirmation in every profile that does not explicitly allow it - so the model
can suggest work all day and cannot set any of it going on its own.

One detail worth the closure it costs: the confirmation dialog shows the permission target, so the
target of `plans.start` is the plan's *title*. "Start: Downloads sortieren" is a question a person
can answer; a twelve-character identifier is not.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from nox.core.logging import get_logger
from nox.plans.book import PlanBook
from nox.plans.model import MAX_STEPS, Plan, PlanStep, unknown_tools
from nox.security.model import Risk
from nox.tools.registry import ToolRegistry, ToolSpec

log = get_logger(__name__)

__all__ = ["register_plan_tools"]


class StepInput(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    why: str = Field(default="", max_length=200)


class ProposeInput(BaseModel):
    """`plans.propose` - a title a person can read and up to a dozen steps."""

    title: str = Field(min_length=1, max_length=80)
    steps: list[StepInput] = Field(min_length=1, max_length=MAX_STEPS)
    stop_on_error: bool = True


class WaitingInput(BaseModel):
    """`plans.waiting` takes no arguments; there are never many."""


class PlanInput(BaseModel):
    plan: str = Field(min_length=1, max_length=40)


class StatusInput(BaseModel):
    """`plans.status` - one plan, or the recent ones when no id is given."""

    plan: str = Field(default="", max_length=40)


def _title_of(book: PlanBook, plan_id: str) -> str:
    """What the confirmation dialog shows. Falls back to the id, which at least identifies it."""
    plan = book.proposed(plan_id)
    return plan.title if plan is not None else plan_id


def _build_propose(book: PlanBook, registry: ToolRegistry) -> ToolSpec:
    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        plan = Plan(
            # The id is ours, never the model's: an id from outside could collide with a waiting
            # plan and quietly replace what the user was about to approve.
            id=uuid4().hex[:12],
            title=str(payload["title"]),
            steps=[PlanStep.model_validate(step) for step in payload["steps"]],
            stop_on_error=bool(payload.get("stop_on_error", True)),
        )
        missing = unknown_tools(plan, registry.names())
        if missing:
            return {
                "ok": False,
                "error": f"no such tool: {', '.join(missing)}",
                "available": registry.names(),
            }
        book.propose(plan)
        log.info("plans.proposed", plan=plan.id, steps=len(plan.steps))
        return {
            "ok": True,
            "plan": plan.review(),
            "note": "nothing has run; the user has to approve this with plans.start",
        }

    return ToolSpec(
        name="plans.propose",
        description=(
            "Write down a sequence of tool calls for the user to approve. Nothing runs. Use this "
            "for work with several steps, or work that should carry on in the background."
        ),
        input_model=ProposeInput,
        risk=Risk.LOW,
        side_effects=False,
        local=True,
        handler=handler,
    )


def _build_start(book: PlanBook) -> ToolSpec:
    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        plan_id = str(payload["plan"])
        try:
            row = await book.start(plan_id)
        except KeyError:
            return {"ok": False, "error": f"there is no plan waiting with the id {plan_id!r}"}
        return {"ok": True, "plan": plan_id, "task": row.id, "status": row.status.value}

    return ToolSpec(
        name="plans.start",
        description=(
            "Start an approved plan. It then runs in the background, survives a restart and pauses "
            "while a game is running. The user is asked before this happens."
        ),
        input_model=PlanInput,
        risk=Risk.HIGH,
        side_effects=True,
        local=True,
        handler=handler,
        targets=lambda payload: _title_of(book, str(payload.get("plan") or "")),
    )


def _build_waiting(book: PlanBook) -> ToolSpec:
    async def handler(_payload: dict[str, Any]) -> dict[str, Any]:
        return {"waiting": [plan.review() for plan in book.waiting()]}

    return ToolSpec(
        name="plans.waiting",
        description="The plans you proposed that the user has not approved yet.",
        input_model=WaitingInput,
        risk=Risk.READ,
        side_effects=False,
        local=True,
        handler=handler,
    )


def _build_status(book: PlanBook) -> ToolSpec:
    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        plan_id = str(payload.get("plan") or "")
        if not plan_id:
            return {"plans": book.history()}
        progress = book.progress(plan_id)
        if progress is None:
            return {"ok": False, "error": f"no plan with the id {plan_id!r} has been started"}
        return progress

    return ToolSpec(
        name="plans.status",
        description=(
            "How far a started plan got, step by step, including which steps failed and why. "
            "Without an id: the plans that ran recently."
        ),
        input_model=StatusInput,
        risk=Risk.READ,
        side_effects=False,
        local=True,
        handler=handler,
    )


def register_plan_tools(registry: ToolRegistry, book: PlanBook) -> None:
    """Register the four `plans.*` tools."""
    for spec in (
        _build_propose(book, registry),
        _build_waiting(book),
        _build_start(book),
        _build_status(book),
    ):
        registry.register(spec)
    log.info("plans.tools_registered", count=4)

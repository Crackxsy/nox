"""What a plan is: a few tool steps, written down so a person can read them before they run.

The shape is deliberately boring. A plan cannot contain a command line, a loop, a condition or
another plan - only tool names and arguments, the same tools that are reachable from the dashboard.
Everything clever a plan could do is a tool somebody had to write and a permission somebody had to
grant, and that is the whole point: a plan makes work survive a restart, not a language model's
reach grow.

Where a plan lives is worth stating plainly. A proposed plan is held in memory and is gone after a
restart, because a proposal is part of a conversation. An approved plan is a row in the task queue,
payload and progress both, so it survives a crash and resumes at the step it reached.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "MAX_STEPS",
    "Plan",
    "PlanStep",
    "StepResult",
    "unknown_tools",
]

#: A plan the user has to read before approving has to stay readable. Twelve steps is already a lot
#: to hold in the head; anything longer is a sign the work wants splitting, not a longer list.
MAX_STEPS = 12

TOOL_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")


class PlanStep(BaseModel):
    """One tool call, with one line explaining why it is in the plan."""

    model_config = ConfigDict(frozen=True)

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    #: For the human reading the plan, not for the machine. Empty is allowed and unhelpful.
    why: str = Field(default="", max_length=200)

    @field_validator("tool")
    @classmethod
    def _looks_like_a_tool_name(cls, value: str) -> str:
        name = value.strip()
        if not TOOL_PATTERN.match(name):
            raise ValueError(f"{value!r} is not a tool name (expected something like 'vault.read')")
        return name


class StepResult(BaseModel):
    """What happened to one step. Kept even when it failed - especially when it failed."""

    model_config = ConfigDict(frozen=True)

    step: int
    tool: str
    ok: bool
    error: str = ""


class Plan(BaseModel):
    """A reviewed sequence of tool calls."""

    model_config = ConfigDict(frozen=True)

    id: str
    title: str = Field(min_length=1, max_length=80)
    steps: list[PlanStep] = Field(min_length=1, max_length=MAX_STEPS)
    #: Stop at the first failure. The default, because a plan whose third step failed has usually
    #: stopped making sense by the fourth - and continuing would hide which step broke it.
    stop_on_error: bool = True
    created_by: str = "companion"

    def review(self) -> dict[str, Any]:
        """The plan as a person reads it before approving: numbered steps, no argument noise."""
        return {
            "id": self.id,
            "title": self.title,
            "steps": [
                {"step": index, "tool": step.tool, "why": step.why}
                for index, step in enumerate(self.steps)
            ],
            "stop_on_error": self.stop_on_error,
        }


def unknown_tools(plan: Plan, known: Iterable[str]) -> list[str]:
    """Step tools that are not registered.

    Checked when a plan is proposed rather than when it runs, so a model that invented a tool name
    hears about it while it can still say something useful to the user.
    """
    available = set(known)
    return sorted({step.tool for step in plan.steps if step.tool not in available})

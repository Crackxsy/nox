"""`skill.learn` and `skill.remove`: what the model may do about its own skills. Which skills
exist, and their tools, is part of `capabilities.list`; the dashboard shows the rest.

`skill.learn` is `high`: it starts a Claude Code session that writes code. The dialog shows the wish
in words (the permission target), and the answer is either "I am building it", an honest reason it
cannot be learned right now, a refusal for something forbidden, or the concrete fix for something
that exists and is only switched off. Activation is deliberately *not* a tool: switching on code
is the user's click in the dashboard, never a model's call. Neither tool is `rememberable`: a
confirmation for one of them is never turned into a session grant.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from pydantic import BaseModel, Field

from nox.capabilities.model import Gap
from nox.security.model import Risk
from nox.skills.gaps import Verdict, assess
from nox.skills.service import SkillService
from nox.tools.registry import ToolRegistry, ToolSpec

__all__ = ["register_skill_tools"]

GapSource = Callable[[], tuple[Iterable[Gap], Iterable[str]]]


class LearnInput(BaseModel):
    intent: str = Field(
        min_length=8,
        max_length=500,
        description="What the user wants to be able to do, in their words.",
    )
    capability: str = Field(
        default="",
        max_length=80,
        description="The tool name or gap that is missing (e.g. 'weather.forecast'), if known.",
    )


class RemoveInput(BaseModel):
    id: str = Field(min_length=2, max_length=40, description="The skill id.")


def register_skill_tools(registry: ToolRegistry, service: SkillService, gaps: GapSource) -> None:
    async def learn(arguments: dict[str, Any]) -> dict[str, Any]:
        payload = LearnInput.model_validate(arguments)
        known_gaps, usable = gaps()
        verdict = assess(
            capability=payload.capability,
            intent=payload.intent,
            gaps=known_gaps,
            usable_tools=usable,
        )
        if verdict.verdict is Verdict.REFUSE:
            return {"ok": False, "refused": True, "reason": verdict.message}
        if verdict.verdict in (Verdict.FIX, Verdict.EXISTS):
            return {"ok": False, "fix": verdict.message}
        return await service.learn(payload.intent, capability=payload.capability, by="companion")

    async def remove(arguments: dict[str, Any]) -> dict[str, Any]:
        payload = RemoveInput.model_validate(arguments)
        return await service.forget(payload.id, by="companion")

    registry.register(
        ToolSpec(
            name="skill.learn",
            description=(
                "Teach yourself a missing capability (a checked plugin the user switches on). "
                "Offer it first in the user's language ('I cannot do that yet - shall I teach "
                "myself?') and call only after a yes."
            ),
            input_model=LearnInput,
            risk=Risk.HIGH,
            handler=learn,
            side_effects=True,
            local=False,
            # Every learning session is its own decision: a remembered "yes" would let the model
            # start coding sessions on its own for the rest of the session.
            rememberable=False,
            # The whole wish (at most 500 characters): the user approves what the builder is
            # told, not a prefix of it with the rest hidden.
            targets=lambda payload: str(payload.get("intent") or ""),
            timeout_s=30.0,
        )
    )
    registry.register(
        ToolSpec(
            name="skill.remove",
            description="Forget a self-built skill (off and deleted).",
            input_model=RemoveInput,
            risk=Risk.LOW,
            handler=remove,
            side_effects=True,
            local=True,
            rememberable=False,
            targets=lambda payload: str(payload.get("id") or ""),
        )
    )

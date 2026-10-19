"""`skill.learn`, `skill.list`, `skill.remove`: what the model may do about its own skills.

`skill.learn` is `high`: it starts a Claude Code session that writes code. The dialog shows the wish
in words (the permission target), and the answer is either "I am building it", an honest reason it
cannot be learned right now, a refusal for something forbidden, or the concrete fix for something
that exists and is only switched off. Activation is deliberately *not* a tool: switching on code
is the user's click in the dashboard, never a model's call.
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
    id: str = Field(min_length=2, max_length=40, description="The skill id, from skill.list.")


class _Empty(BaseModel):
    pass


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

    async def list_skills(_arguments: dict[str, Any]) -> dict[str, Any]:
        status = service.status()
        return {
            "available": status["available"],
            "reason": status["reason"],
            "skills": [
                {
                    "id": s["id"],
                    "intent": s["intent"],
                    "state": s["state"],
                    "tools": [t["name"] for t in (s.get("summary") or {}).get("tools", [])],
                }
                for s in status["skills"]
            ],
        }

    async def remove(arguments: dict[str, Any]) -> dict[str, Any]:
        payload = RemoveInput.model_validate(arguments)
        return await service.forget(payload.id, by="companion")

    registry.register(
        ToolSpec(
            name="skill.learn",
            description=(
                "Teach yourself a missing capability: builds a small, checked plugin that the user "
                "then switches on in the dashboard. Offer it first ('Kann ich noch nicht - soll "
                "ich "
                "es mir beibringen?') and only call it after the user said yes. Not for anything "
                "forbidden, and not for something that exists but is switched off - say the fix."
            ),
            input_model=LearnInput,
            risk=Risk.HIGH,
            handler=learn,
            side_effects=True,
            local=False,
            targets=lambda payload: str(payload.get("intent") or "")[:200],
            timeout_s=30.0,
        )
    )
    registry.register(
        ToolSpec(
            name="skill.list",
            description="The skills you built for yourself and their state.",
            input_model=_Empty,
            risk=Risk.READ,
            handler=list_skills,
            side_effects=False,
            local=True,
        )
    )
    registry.register(
        ToolSpec(
            name="skill.remove",
            description="Forget one self-built skill (switches it off and deletes it).",
            input_model=RemoveInput,
            risk=Risk.LOW,
            handler=remove,
            side_effects=True,
            local=True,
            targets=lambda payload: str(payload.get("id") or ""),
        )
    )

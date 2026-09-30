"""The `extend.*` tools: ask for a change to Nox, and look at what came back.

`extend.propose` is high risk, so every profile that has not explicitly allowed it opens a
confirmation - and the confirmation shows the *intent*, because "write a branch that: adds a tool
for reading my calendar" is a question a person can answer and an identifier is not.

There is deliberately no `extend.apply`. A proposal is a branch; merging it and restarting Nox on
the result are things a person does with their own git. That is what keeps "Nox can extend itself"
from meaning "Nox can change what it is while you are not looking".
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from nox.core.logging import get_logger
from nox.extend.runner import ProposalRunner
from nox.security.model import Risk
from nox.tools.registry import ToolRegistry, ToolSpec

log = get_logger(__name__)

__all__ = ["register_extend_tools"]

#: Enough for a real description, short enough that the confirmation dialog stays readable.
MAX_INTENT = 2000


class ProposeInput(BaseModel):
    """`extend.propose {intent}` - what the change should do, in words."""

    intent: str = Field(min_length=10, max_length=MAX_INTENT)


class StatusInput(BaseModel):
    """`extend.status {proposal}` - one proposal, or the recent ones when no id is given."""

    proposal: str = Field(default="", max_length=40)


def register_extend_tools(registry: ToolRegistry, runner: ProposalRunner) -> None:
    """Register `extend.propose` and `extend.status`."""

    async def propose(payload: dict[str, Any]) -> dict[str, Any]:
        proposal = await runner.propose(str(payload["intent"]))
        return proposal.as_dict()

    async def status(payload: dict[str, Any]) -> dict[str, Any]:
        wanted = str(payload.get("proposal") or "")
        if not wanted:
            return {"proposals": [p.as_dict() for p in runner.all()]}
        found = runner.get(wanted)
        if found is None:
            return {"ok": False, "error": f"there is no proposal with the id {wanted!r}"}
        return found.as_dict()

    registry.register(
        ToolSpec(
            name="extend.propose",
            description=(
                "Ask for a change to Nox's own source. It is written on a new branch in the "
                "checkout the user configured, the tests are run, and you get the diff - nothing "
                "is merged and Nox does not restart itself. Say what the change should do."
            ),
            input_model=ProposeInput,
            risk=Risk.HIGH,
            side_effects=True,
            local=True,
            handler=propose,
            # The dialog shows the target, so the target is what the user needs to read.
            targets=lambda payload: str(payload.get("intent") or "")[:120],
        )
    )
    registry.register(
        ToolSpec(
            name="extend.status",
            description="What came of a proposal: which files changed and what the tests said.",
            input_model=StatusInput,
            risk=Risk.READ,
            side_effects=False,
            local=True,
            handler=status,
        )
    )
    log.info("extend.tools_registered", count=2)

"""The two `capabilities.*` tools, which are what let Nox answer honestly about itself.

`capabilities.list` deliberately does **not** repeat the tool descriptions - the model already has
those in its prompt, and sending them twice would waste the context this tool exists to inform. It
sends the *states*: which tools are usable right now, which would ask first, which are refused and
by which rule, which exist but cannot reach what they need, and what is missing entirely.

`capabilities.check` answers one question - "can you do X?" - and its most valuable answer is the
third one. A name that is neither a tool nor a listed gap gets "I have no entry for that", not a
no. Turning ignorance into a denial is how an assistant ends up confidently wrong about itself.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from nox.capabilities.model import Capability, CapabilityReport, CapabilityState, Gap
from nox.core.logging import get_logger
from nox.security.model import Decision, Risk
from nox.tools.registry import ToolRegistry, ToolSpec

log = get_logger(__name__)

__all__ = ["ReportProvider", "register_capability_tools"]

#: Built fresh per call: the answer depends on the profile, the mode and the privacy state, all of
#: which change while Nox runs. A cached report would be a confident statement about the past.
ReportProvider = Callable[[], CapabilityReport]


class CapabilityListInput(BaseModel):
    """`capabilities.list` takes no arguments; the whole picture is the point."""


class CapabilityCheckInput(BaseModel):
    """`capabilities.check {name}` - a tool name, or the kind of thing the user asked for."""

    name: str = Field(min_length=1, max_length=120)


def _needs(capability: Capability) -> dict[str, Any]:
    """One entry in a list of things that are not simply usable, with the reason kept."""
    entry: dict[str, Any] = {"name": capability.name, "risk": capability.risk.value}
    if capability.reason:
        entry["reason"] = capability.reason
    return entry


def _gap(gap: Gap) -> dict[str, Any]:
    return {"name": gap.name, "what": gap.what, "why": gap.reason.value, "detail": gap.detail}


def _summary(report: CapabilityReport) -> dict[str, Any]:
    confirm = [c for c in report.capabilities if c.decision is Decision.CONFIRM]
    denied = [c for c in report.capabilities if c.decision is Decision.DENY]
    unreachable = [
        c
        for c in report.capabilities
        if c.decision is not Decision.DENY and c.state is not CapabilityState.AVAILABLE
    ]
    return {
        "mode": report.mode,
        "profile": report.profile,
        "privacy_mode": report.privacy_mode,
        "usable": [c.name for c in report.usable],
        "asks_first": [_needs(c) for c in confirm],
        "refused_here": [{"name": c.name, "rule": c.rule} for c in denied],
        "unreachable": [_needs(c) for c in unreachable],
        # Named separately because "yes, but only inside the vault" is not the same answer as "yes".
        "depends_on_target": [c.name for c in report.capabilities if c.target_dependent],
        "missing": [_gap(g) for g in report.gaps],
    }


def _matches(name: str, candidate: str) -> bool:
    """`file` finds `file.delete`, and `vault.read` finds itself.

    Prefix matching on dotted segments only - `fi` must not match `file.read`, because a guess that
    looks like a hit is worse than a miss the model can react to.
    """
    if name == candidate:
        return True
    return candidate.startswith(f"{name}.") or name.startswith(f"{candidate}.")


def _answer(report: CapabilityReport, name: str) -> dict[str, Any]:
    wanted = name.strip().lower().replace(" ", ".")
    exact = report.find(wanted)
    if exact is not None:
        return {
            "known": True,
            "name": exact.name,
            "possible": exact.usable,
            "decision": exact.decision.value,
            "rule": exact.rule,
            "state": exact.state.value,
            "reason": exact.reason,
            "depends_on_target": exact.target_dependent,
        }
    for gap in report.gaps:
        if _matches(wanted, gap.name):
            return {"known": True, "possible": False, **_gap(gap)}
    near = sorted(
        {c.name for c in report.capabilities if _matches(wanted, c.name)}
        | {g.name for g in report.gaps if _matches(wanted, g.name)}
    )
    if near:
        return {"known": False, "name": wanted, "related": near}
    # The honest third answer. Not a no.
    return {
        "known": False,
        "name": wanted,
        "note": "no entry for this, so I cannot say whether it is possible without looking",
    }


def _build_list(report_of: ReportProvider) -> ToolSpec:
    async def handler(_payload: dict[str, Any]) -> dict[str, Any]:
        return _summary(report_of())

    return ToolSpec(
        name="capabilities.list",
        description=(
            "What you can actually do right now: which of your tools are usable, which would ask "
            "the user first, which are refused in this profile, and what you do not have at all. "
            "Use this before promising or refusing something."
        ),
        input_model=CapabilityListInput,
        risk=Risk.READ,
        side_effects=False,
        local=True,
        handler=handler,
    )


def _build_check(report_of: ReportProvider) -> ToolSpec:
    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        return _answer(report_of(), str(payload["name"]))

    return ToolSpec(
        name="capabilities.check",
        description=(
            "Whether one specific thing is possible right now. Give a tool name such as "
            "'file.delete'. An answer with known=false means there is no entry for it - say that, "
            "do not turn it into a no."
        ),
        input_model=CapabilityCheckInput,
        risk=Risk.READ,
        side_effects=False,
        local=True,
        handler=handler,
        targets=lambda payload: str(payload.get("name") or ""),
    )


def register_capability_tools(registry: ToolRegistry, report_of: ReportProvider) -> None:
    """Register `capabilities.list` and `capabilities.check`."""
    for spec in (_build_list(report_of), _build_check(report_of)):
        registry.register(spec)
    log.info("capabilities.tools_registered", count=2)

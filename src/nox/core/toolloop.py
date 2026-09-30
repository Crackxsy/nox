"""`ToolGate`: the one place a conversation can reach a tool.

The orchestrator streams text and knows nothing about permissions; the executor checks permissions
and knows nothing about conversations. This sits between them and is deliberately small - it parses
one directive, runs one tool, and hands back one sentence for the model to read.

Two decisions worth keeping:

* **The offer comes from the capability report, not from the registry.** A tool the active profile
  denies is never mentioned to the model, which is both cheaper and more honest than offering it and
  refusing the call. A tool that would ask the user first *is* offered, marked as such, because
  hiding it would take the choice away from the person the dialog is for.
* **Nothing here decides what may run.** Every call goes through the executor under the `companion`
  agent, so it meets the same permission check, audit entry and kill switch as a click in the
  dashboard. A model that is talked into asking for something still has to get past all of that.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from nox.ai.tooluse import OfferedTool, ToolCall, parse_directive, render_offer, render_result
from nox.core.logging import get_logger

log = get_logger(__name__)

__all__ = ["ToolGate", "ToolOutcome", "ToolRound"]

#: How many tool rounds one turn may take before Nox answers with what it has. Three is enough for
#: "look something up, act on it, report" and small enough that a confused model cannot spin.
DEFAULT_MAX_ROUNDS = 3


class ToolOutcome(Protocol):
    """What the executor returns. Structural, so this module imports nothing from `nox.tools`."""

    ok: bool
    data: dict[str, Any] | None
    error: str | None


Offer = Callable[[], Sequence[OfferedTool]]
Call = Callable[[str, dict[str, Any]], Awaitable[ToolOutcome]]


@dataclass(frozen=True, slots=True)
class ToolRound:
    """One round: what to hand back to the model, and what actually happened.

    The tool name is carried separately from the message because the turn record and the dashboard
    want it as data, and digging it back out of a sentence written for a language model would be
    exactly the kind of parsing this code exists to avoid.
    """

    feedback: str
    tool: str = ""
    ok: bool = False


@dataclass(slots=True)
class ToolGate:
    """Tool use for one orchestrator. `None` there means conversations get no tools at all."""

    offer: Offer
    call: Call
    max_rounds: int = DEFAULT_MAX_ROUNDS
    #: Names offered in the current turn, so a directive can be checked against them.
    _allowed: list[str] = field(default_factory=list)

    def prompt(self) -> str:
        """The tool section for this turn's system message, and the names it permits.

        Refreshed per turn because the profile, the mode and the privacy state can all change
        between two sentences, and an offer made from a stale report is a promise Nox cannot keep.
        """
        try:
            tools = list(self.offer())
        except Exception as exc:  # noqa: BLE001 - no offer is a worse turn, not a failed one
            log.warning("toolloop.offer_failed", error=f"{type(exc).__name__}: {exc}")
            tools = []
        self._allowed = [tool.name for tool in tools]
        return render_offer(tools)

    async def advance(self, text: str) -> ToolRound | None:
        """`None` when `text` is an ordinary answer; otherwise the round to hand back.

        A refusal, a timeout and a crash all come back as a sentence rather than an exception: the
        model is mid-conversation and the user is waiting, so "that did not work, here is why" is
        the only useful shape.
        """
        parsed = parse_directive(text, self._allowed)
        if parsed is None:
            return None
        if isinstance(parsed, str):
            log.info("toolloop.directive_rejected", reason=parsed)
            return ToolRound(feedback=parsed)
        return await self._run(parsed)

    async def _run(self, requested: ToolCall) -> ToolRound:
        try:
            outcome = await self.call(requested.name, requested.arguments)
        except Exception as exc:  # noqa: BLE001 - a broken tool must not end the conversation
            log.warning(
                "toolloop.call_raised", tool=requested.name, error=f"{type(exc).__name__}: {exc}"
            )
            return ToolRound(
                feedback=render_result(requested.name, ok=False, error=type(exc).__name__),
                tool=requested.name,
            )
        log.info("toolloop.called", tool=requested.name, ok=outcome.ok)
        return ToolRound(
            feedback=render_result(
                requested.name, ok=outcome.ok, data=outcome.data, error=outcome.error or ""
            ),
            tool=requested.name,
            ok=outcome.ok,
        )

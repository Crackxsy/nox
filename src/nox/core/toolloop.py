"""`ToolLoop`: the one place a conversation can reach a tool.

The orchestrator streams text and knows nothing about permissions; the executor checks permissions
and knows nothing about conversations. This sits between them. Per chat turn it opens an
`ActiveTurn` - the offer for that turn, a `TurnBudget`, and a random token - and every tool call of
that turn, whichever way it arrives, goes through `ToolLoop.execute`:

* native calls from a model Nox drives itself (Ollama's `tool_calls`, and the text-sentinel
  fallback for models without tool support, which the provider turns into the same calls),
* calls Claude Code makes through Nox's MCP bridge (`nox.mcp.gate` looks the turn up by token).

Decisions worth keeping:

* **The offer comes from the capability report, not from the registry.** A tool the active profile
  denies is never offered, which is both cheaper and more honest than offering it and refusing the
  call. A tool that would ask the user first *is* offered, marked as such, because hiding it would
  take the choice away from the person the dialog is for.
* **Nothing here decides what may run.** Every call goes through the executor under the `companion`
  agent, so it meets the same permission check, audit entry and kill switch as a click in the
  dashboard. This module only adds what a conversation needs on top: the call must be in this
  turn's offer, the turn must still be open, and the budget must allow it.
* **A turn's token dies with the turn.** A call that arrives after the turn ended (a model that kept
  going after barge-in, a stale bridge) finds no turn and is refused.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from nox.ai.base import ToolDefinition
from nox.ai.tooluse import OfferedTool, render_unavailable
from nox.ai.tooluse import Unavailable as UnavailableItem
from nox.core.logging import get_logger
from nox.core.turnbudget import TurnBudget

log = get_logger(__name__)

__all__ = ["ActiveTurn", "ToolEvent", "ToolLoop", "ToolOutcome", "ToolRun"]

#: Error codes a refused call carries, so the model and the tests can tell the cases apart.
REFUSED_NOT_OFFERED = "tool.not_offered"
REFUSED_TURN_OVER = "turn.closed"
REFUSED_BUDGET = "turn.budget"
REFUSED_WITHDRAWN = "tool.withdrawn"


class ToolOutcome(Protocol):
    """What the executor returns. Structural, so this module imports nothing from `nox.tools`."""

    ok: bool
    data: dict[str, Any] | None
    error: str | None


Offer = Callable[[], Sequence[OfferedTool]]
Call = Callable[[str, dict[str, Any]], Awaitable[ToolOutcome]]
Unavailable = Callable[[], Sequence[UnavailableItem]]
BudgetFactory = Callable[[], TurnBudget]
TaintSetter = Callable[[bool], None]

#: Tool families whose result may carry content Nox did not author. After one of them runs, the
#: turn is "tainted": a remembered grant above LOW falls back to a fresh confirmation, so a web
#: page or an MCP result cannot silently trigger a higher-risk action it names (prompt injection).
TAINTING_PREFIXES: tuple[str, ...] = ("web", "browser", "mcp", "file", "vision", "screen")


def _is_tainting(name: str) -> bool:
    return name.split(".", 1)[0] in TAINTING_PREFIXES


@dataclass(frozen=True, slots=True)
class ToolRun:
    """The result of one call as the model will read it, plus whether it ran at all."""

    name: str
    ok: bool
    data: dict[str, Any] | None = None
    error: str = ""
    #: True when Nox refused before the executor saw the call (not offered, turn over, budget).
    refused: bool = False


@dataclass(frozen=True, slots=True)
class ToolEvent:
    kind: Literal["started", "finished"]
    name: str
    ok: bool = False


@dataclass(eq=False)
class ActiveTurn:
    """One turn's tools. Created by `ToolLoop.begin`, closed by `ToolLoop.end`."""

    turn_id: str
    token: str
    offered: dict[str, OfferedTool]
    budget: TurnBudget
    #: Tools that actually ran, in order (refused calls are not in here).
    used: list[str] = field(default_factory=list)
    listeners: list[Callable[[ToolEvent], None]] = field(default_factory=list)
    closed: bool = False
    #: The MCP bridge the turn's token was handed to (`nox.mcp.gate`); empty for a turn whose
    #: calls come back through the orchestrator.
    client: str = ""
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)

    def definitions(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name=tool.name,
                description=tool.description,
                input_schema=dict(tool.schema_),
                asks_first=tool.asks_first,
            )
            for tool in self.offered.values()
        ]

    def emit(self, event: ToolEvent) -> None:
        for listener in list(self.listeners):
            try:
                listener(event)
            except Exception as exc:  # noqa: BLE001 - a listener must not break the call
                log.warning("toolloop.listener_failed", error=f"{type(exc).__name__}: {exc}")


@dataclass(slots=True)
class ToolLoop:
    """Tool use for one orchestrator. `None` there means conversations get no tools at all."""

    offer: Offer
    call: Call
    #: What cannot be done right now, and why; rendered into the turn context.
    unavailable: Unavailable | None = None
    budget_factory: BudgetFactory = TurnBudget
    #: Marks the turn tainted/clean in the permission engine (set when untrusted content enters).
    taint: TaintSetter | None = None
    _turns: dict[str, ActiveTurn] = field(default_factory=dict)

    # -- offer -----------------------------------------------------------------------------------

    def current_offer(self) -> list[OfferedTool]:
        """The tools usable right now. Refreshed per call: profile, mode and privacy all change
        between two sentences, and an offer made from a stale report is a promise Nox cannot keep.
        """
        try:
            return list(self.offer())
        except Exception as exc:  # noqa: BLE001 - no offer is a worse turn, not a failed one
            log.warning("toolloop.offer_failed", error=f"{type(exc).__name__}: {exc}")
            return []

    def current_offer_names(self) -> list[str]:
        return [tool.name for tool in self.current_offer()]

    def caveats(self) -> str:
        """The "not available right now" section for the turn context, or empty."""
        if self.unavailable is None:
            return ""
        try:
            return render_unavailable(list(self.unavailable()))
        except Exception as exc:  # noqa: BLE001 - the offer still stands without the caveats
            log.warning("toolloop.unavailable_failed", error=f"{type(exc).__name__}: {exc}")
            return ""

    # -- turns -------------------------------------------------------------------------------------

    def begin(self, turn_id: str) -> ActiveTurn:
        turn = ActiveTurn(
            turn_id=turn_id,
            token=secrets.token_urlsafe(32),
            offered={tool.name: tool for tool in self.current_offer()},
            budget=self.budget_factory(),
        )
        self._turns[turn.token] = turn
        if self.taint is not None:
            # A fresh turn starts clean; a grant remembered in an earlier turn is trusted again.
            self.taint(False)
        return turn

    def by_token(self, token: str) -> ActiveTurn | None:
        """The open turn this token belongs to, compared in constant time against each token."""
        found: ActiveTurn | None = None
        for candidate, turn in list(self._turns.items()):
            if secrets.compare_digest(candidate.encode(), token.encode()):
                found = turn
        return found

    def open_turns(self) -> list[ActiveTurn]:
        return list(self._turns.values())

    async def end(self, turn: ActiveTurn) -> None:
        """Close the turn: its token stops working and calls still running are cancelled."""
        turn.closed = True
        self._turns.pop(turn.token, None)
        running = [task for task in turn.tasks if not task.done()]
        for task in running:
            task.cancel()
        for task in running:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def end_all(self) -> None:
        """Kill switch: every open turn ends now."""
        for turn in list(self._turns.values()):
            await self.end(turn)

    # -- calls -------------------------------------------------------------------------------------

    async def execute(self, turn: ActiveTurn, name: str, arguments: dict[str, Any]) -> ToolRun:
        """Run one call of `turn`. Refusals, timeouts and crashes come back as a `ToolRun`, never
        as an exception: the model is mid-conversation and "that did not work, here is why" is the
        only useful shape."""
        if turn.closed:
            return ToolRun(name, ok=False, error=REFUSED_TURN_OVER, refused=True)
        if name not in turn.offered:
            log.info("toolloop.not_offered", tool=name)
            return ToolRun(name, ok=False, error=REFUSED_NOT_OFFERED, refused=True)
        if name not in self.current_offer_names():
            # The profile, mode or privacy changed during the turn: the offer it started with
            # no longer holds for this tool.
            log.info("toolloop.no_longer_offered", tool=name)
            return ToolRun(name, ok=False, error=REFUSED_WITHDRAWN, refused=True)
        refusal = turn.budget.admit(name, arguments)
        if refusal is not None:
            log.info("toolloop.budget_refused", tool=name, calls=turn.budget.calls)
            return ToolRun(name, ok=False, error=f"{REFUSED_BUDGET}: {refusal}", refused=True)
        turn.emit(ToolEvent("started", name))
        task = asyncio.ensure_future(self.call(name, arguments))
        turn.tasks.add(task)
        try:
            outcome = await task
        except asyncio.CancelledError:
            turn.emit(ToolEvent("finished", name, ok=False))
            if turn.closed and not _current_task_cancelling():
                return ToolRun(name, ok=False, error=REFUSED_TURN_OVER, refused=True)
            raise
        except Exception as exc:  # noqa: BLE001 - a broken tool must not end the conversation
            log.warning("toolloop.call_raised", tool=name, error=f"{type(exc).__name__}: {exc}")
            turn.budget.record(name, arguments, ok=False)
            turn.emit(ToolEvent("finished", name, ok=False))
            return ToolRun(name, ok=False, error=type(exc).__name__)
        finally:
            turn.tasks.discard(task)
        turn.budget.record(name, arguments, ok=outcome.ok)
        turn.used.append(name)
        if outcome.ok and self.taint is not None and _is_tainting(name):
            # Untrusted content just entered the turn; higher-risk remembered grants now re-confirm.
            self.taint(True)
        turn.emit(ToolEvent("finished", name, ok=outcome.ok))
        log.info("toolloop.called", tool=name, ok=outcome.ok)
        return ToolRun(name, ok=outcome.ok, data=outcome.data, error=outcome.error or "")


def _current_task_cancelling() -> bool:
    """Whether the caller itself is being cancelled (as opposed to only the inner call)."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0

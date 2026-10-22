"""ToolSpec / ToolRegistry: declarative tool catalogue (Tool Model "Tool definition").

Tools are registered once at composition time (core boot, plugin load) and looked up by name at
call time by `nox.tools.executor.ToolExecutor`. The registry never runs anything itself and never
knows about permissions; it only stores specs and can describe them (name/description/schema/risk)
for the model prompt and the dashboard, without exposing handlers.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from nox.security.model import Risk

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
Targets = Callable[[dict[str, Any]], str]


@dataclass(frozen=True, slots=True)
class CallScope:
    """The permission `action` and `risk` of one particular call.

    Most tools have one risk whatever they are asked to do. A generic tool does not: an OBS
    request that reads the scene list and one that deletes a scene go through the same tool, and
    must not share one decision - nor one remembered "allow". `action` is what the permission
    request, the audit entry and a session grant carry, so a distinct action per kind of call is
    also what keeps a remembered answer to that kind.
    """

    action: str
    risk: Risk
    #: What the confirmation dialog and the audit entry name as the call's target; empty means
    #: the tool's own `targets`.
    target: str = ""
    #: The node the call runs on (`nox.node.router`); "" for this core.
    device: str = ""
    #: High risk on a node: the person at that device confirms it there (owner decision D1),
    #: so the core does not ask anyone itself. A refusal by the core's rules still refuses.
    confirm_on_device: bool = False
    #: False: no "remember" answer for this call, whatever the tool allows otherwise (a write on
    #: a node is asked every time).
    rememberable: bool = True


Scope = Callable[[dict[str, Any]], CallScope]


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolSpec:
    """One row of the tool catalogue (Tool Model table).

    `targets` derives the permission `target` (path, scene name, host, ...) from the validated
    input; `None` means the tool carries no meaningful target (e.g. `time.now`). `local=True` means
    the tool never leaves the machine (dashboard/description metadata only - the permission engine
    itself classifies "cloud" tools by name pattern, see `nox.security.permissions`).
    """

    name: str
    description: str
    input_model: type[BaseModel]
    risk: Risk
    handler: Handler
    side_effects: bool = True
    local: bool = True
    #: Whether a CONFIRM on this tool may be remembered as a session grant. False for tools where
    #: "always allow" would be dangerous however scoped: `package.*`, `mic.record_long`, `skill.*`.
    #: `shell.run` stays True but remembers per command pattern (a `cmd:` grant), not the tool.
    rememberable: bool = True
    targets: Targets | None = None
    #: Whether a call sends data off the machine (a declared egress, a download). True, or a
    #: function of the validated input. The permission engine treats such a call like a cloud tool:
    #: refused in privacy mode OFFLINE/PRIVATE, and confirmed again once the conversation holds
    #: untrusted content (exfiltration).
    network: bool | Callable[[dict[str, Any]], bool] = False
    #: The call's free text (query, URL, argv) that the permission engine compares with the
    #: untrusted content of a tainted conversation, to let a user-directed lookup run without a
    #: confirmation (`nox.security.taint`). Only `web.search`, `web.fetch` and a curated read-only
    #: `shell.run` can benefit; for every other tool it is ignored.
    lookup: Targets | None = None
    #: How long one call may take, when the executor's default is too short by design - a coding
    #: session takes minutes. None means the executor's default.
    timeout_s: float | None = None
    #: Derives the permission action and risk from the validated input, for a tool whose risk
    #: depends on what it is asked to do (see `CallScope`). None means the tool's own action
    #: (the part of its name after the first dot) and `risk`. Only core code supplies one: a
    #: plugin cannot lower the risk of its own calls.
    scope: Scope | None = None
    #: Awaited before `scope`, bounded by the tool itself: lets state the scope reads settle
    #: first (a device that has just connected and is still announcing its tools).
    prepare: Callable[[dict[str, Any]], Awaitable[None]] | None = None


class ToolDescription(BaseModel):
    """What the model/dashboard get for one tool - never the handler."""

    name: str
    description: str
    input_schema: dict[str, Any]
    risk: Risk
    side_effects: bool
    local: bool


class ToolRegistry:
    """In-memory catalogue of `ToolSpec`s. Not thread-safe (single event loop, like the bus)."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        # `-` is the wire form of `.` (`nox.tools.names`); a name containing it would make the
        # mapping ambiguous, and a model could reach one tool by spelling another.
        if "-" in spec.name:
            raise ValueError(f"tool names may not contain '-': {spec.name!r}")
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name!r}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def describe(self) -> list[ToolDescription]:
        """List for the model prompt / dashboard. Never includes handlers."""
        return [
            ToolDescription(
                name=spec.name,
                description=spec.description,
                input_schema=spec.input_model.model_json_schema(),
                risk=spec.risk,
                side_effects=spec.side_effects,
                local=spec.local,
            )
            for spec in (self._tools[name] for name in sorted(self._tools))
        ]

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

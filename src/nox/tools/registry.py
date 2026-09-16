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
    targets: Targets | None = None


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

"""What a `home.*` call would really touch: the answer behind the `home.effect` preflight tool.

`home.scene` names one entity and changes several; `home.script` and `home.automation.trigger` run
whatever their author wrote; a relay that opens the garage is often a plain `switch`, and a garage
door without a device class is a plain `cover`. The core asks this module, through the tool
preflight (`nox.tools.executor`), before any of those calls runs:

* a scene is judged by its member list (Home Assistant's `entity_id` attribute on the scene),
* a script or automation by its definition (`script/config`, `automation/config`), cached for
  :data:`CONFIG_TTL_S` seconds,
* a switch or cover by its device class and name.

The verdict is the classification in `nox.home.boundary`: `allow`, `confirm` (with the entities it
would change, so the confirmation names them) or `deny` (it would touch a lock, an alarm panel, a
valve or a garage door). Anything that cannot be read - a scene without a member list, a script
whose definition the token may not read - is `confirm`, never `allow`.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from pydantic import BaseModel, Field, ValidationError

from nox.home.boundary import (
    Effect,
    EffectFinding,
    config_effect,
    config_references,
    domain_of,
    entity_effect,
    members_effect,
)

from .inventory import Inventory
from .models import CoverInput, SceneInput, ScriptInput, SwitchInput
from .ws_client import HomeCommandError

#: How long one script/automation definition is reused. Editing a script in Home Assistant is rare;
#: reading its definition on every call would double the round trips of a dashboard click.
CONFIG_TTL_S: Final[float] = 60.0

#: Home Assistant's WebSocket command that returns one definition, per domain.
CONFIG_COMMANDS: Final[dict[str, str]] = {
    "script": "script/config",
    "automation": "automation/config",
}

#: Upper bound on the entities one verdict names (the confirmation dialog shows them).
MAX_VERDICT_TARGETS: Final[int] = 20

_DECISIONS: Final[dict[Effect, str]] = {
    Effect.SAFE: "allow",
    Effect.CONFIRM: "confirm",
    Effect.FORBIDDEN: "deny",
}

CommandFn = Callable[[str, Mapping[str, Any]], Awaitable[Any]]


class EffectInput(BaseModel):
    """`home.effect`: the tool the core is about to run, and the input it will run it with."""

    tool: str = Field(max_length=64)
    input: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Cached:
    config: Any
    fetched_at: float


class DefinitionCache:
    """Script and automation definitions, read on demand and kept for `CONFIG_TTL_S`."""

    def __init__(self, command: CommandFn, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._command = command
        self._clock = clock
        self._entries: dict[str, _Cached] = {}

    async def get(self, entity_id: str) -> Any | None:
        """The definition of one script/automation, or `None` when Home Assistant would not say."""
        now = self._clock()
        cached = self._entries.get(entity_id)
        if cached is not None and now - cached.fetched_at < CONFIG_TTL_S:
            return cached.config
        command = CONFIG_COMMANDS.get(domain_of(entity_id))
        if command is None:
            return None
        try:
            result = await self._command(command, {"entity_id": entity_id})
        except (HomeCommandError, ConnectionError, TimeoutError):
            return None
        config = result.get("config") if isinstance(result, Mapping) else None
        if config is None:
            return None
        self._entries[entity_id] = _Cached(config, now)
        return config

    def clear(self) -> None:
        self._entries.clear()


def verdict(finding: EffectFinding, subject: list[str]) -> dict[str, Any]:
    """A `PreflightVerdict`-shaped answer: the entities named first, then what they would change."""
    targets = list(dict.fromkeys((*subject, *finding.entities)))[:MAX_VERDICT_TARGETS]
    return {"decision": _DECISIONS[finding.effect], "targets": targets, "reason": finding.reason}


def refusal(reason: str) -> dict[str, Any]:
    return {"decision": "deny", "targets": [], "reason": reason}


class EffectChecker:
    """Classifies one pending call against the current inventory (see the module docstring)."""

    def __init__(self, definitions: DefinitionCache) -> None:
        self.definitions = definitions

    async def check(
        self, tool: str, payload: Mapping[str, Any], inventory: Inventory
    ) -> tuple[EffectFinding, list[str]]:
        """(finding, the entities the call names). Raises `ValueError` for input or tool it cannot
        judge - which the caller turns into a refusal."""
        try:
            if tool == "home.scene":
                scene = SceneInput.model_validate(payload).entity_id
                return self._scene(scene, inventory), [scene]
            if tool in ("home.script", "home.automation.trigger"):
                entity_id = ScriptInput.model_validate(payload).entity_id
                return await self._definition(entity_id, inventory), [entity_id]
            if tool == "home.switch":
                ids = list(SwitchInput.model_validate(payload).entity_ids)
                return self._entities(ids, inventory), ids
            if tool == "home.cover":
                ids = list(CoverInput.model_validate(payload).entity_ids)
                return self._entities(ids, inventory), ids
        except ValidationError as exc:
            raise ValueError(f"invalid input for {tool}: {exc.errors()[0]['msg']}") from None
        raise ValueError(f"no effect check exists for {tool!r}")

    def _scene(self, scene: str, inventory: Inventory) -> EffectFinding:
        hidden = inventory.hidden_reason(scene)
        if hidden is not None:
            return EffectFinding(Effect.FORBIDDEN, (scene,), hidden)
        row = inventory.by_id(scene)
        if row is None:  # the call itself answers "not an entity Nox can see"
            return EffectFinding()
        if row.members is None:
            return EffectFinding(
                Effect.CONFIRM,
                (scene,),
                "Home Assistant does not say which devices this scene changes",
            )
        return members_effect(row.members, inventory.effect_attributes, inventory.names)

    async def _definition(self, entity_id: str, inventory: Inventory) -> EffectFinding:
        config = await self.definitions.get(entity_id)
        if config is None:
            return EffectFinding(
                Effect.CONFIRM,
                (entity_id,),
                "its definition could not be read (reading it needs an administrator token)",
            )
        return config_effect(
            config_references(config), inventory.effect_attributes, inventory.names
        )

    @staticmethod
    def _entities(entity_ids: list[str], inventory: Inventory) -> EffectFinding:
        finding = EffectFinding()
        for entity_id in entity_ids:
            finding = finding.merge(
                entity_effect(
                    entity_id,
                    inventory.effect_attributes.get(entity_id),
                    inventory.names.get(entity_id, ""),
                )
            )
        return finding

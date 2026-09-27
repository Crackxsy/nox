"""The `presets.*` tool catalogue.

Three tools, and the split between them is the point:

* `presets.list` (read) lets the model find out which presets exist. It reports what a preset is
  called and what kinds of step it has - never the command line of an action, because those paths
  contain the user's account name and the model has no use for them.
* `presets.activate` (medium) takes an identifier and nothing else. This is the entire surface a
  language model has over presets: it can ask for one by name, and the configuration decides what
  that means.
* `presets.run_action` (high) starts one registered program. The runner routes `run` steps through
  it so that a program start carries the same permission check and audit entry as any other tool
  call. It is high risk for the same reason `home.script` is: the user put the command there, and
  a program can do whatever its author wrote into it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from nox.core.config.presets import ID_PATTERN, PresetsConfig
from nox.core.logging import get_logger
from nox.presets.actions import run_action
from nox.security.model import Risk
from nox.tools.registry import ToolRegistry, ToolSpec

if TYPE_CHECKING:
    from nox.presets.engine import PresetRunner

log = get_logger(__name__)

__all__ = ["register_preset_tools"]

Settings = Callable[[], PresetsConfig]


class PresetListInput(BaseModel):
    """`presets.list` takes no arguments; presets are few enough to list in full."""


class PresetActivateInput(BaseModel):
    """`presets.activate {preset}` - an identifier, never a command."""

    preset: str = Field(pattern=ID_PATTERN)


class RunActionInput(BaseModel):
    """`presets.run_action {action}` - an identifier, never a command."""

    action: str = Field(pattern=ID_PATTERN)


def _build_list(settings: Settings) -> ToolSpec:
    async def handler(_payload: dict[str, Any]) -> dict[str, Any]:
        config = settings()
        return {
            "enabled": config.enabled,
            "presets": [
                {
                    "id": preset.id,
                    "name": preset.name,
                    "enabled": preset.enabled,
                    "phrases": list(preset.triggers.phrases),
                    "steps": [step.kind for step in preset.steps],
                }
                for preset in config.items
            ],
            # Names only. What a registered action actually runs stays in the configuration.
            "actions": [{"id": action.id, "name": action.name} for action in config.actions],
        }

    return ToolSpec(
        name="presets.list",
        description="List the presets and registered actions the user has configured.",
        input_model=PresetListInput,
        risk=Risk.READ,
        side_effects=False,
        local=True,
        handler=handler,
    )


def _build_activate(runner: PresetRunner) -> ToolSpec:
    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        preset_id = str(payload["preset"])
        try:
            run = await runner.activate(preset_id, trigger="model")
        except KeyError:
            return {"ok": False, "error": f"there is no preset named {preset_id!r}"}
        return run.as_dict()

    return ToolSpec(
        name="presets.activate",
        description=(
            "Activate one preset by its identifier. The steps it runs are fixed in the user's "
            "configuration and cannot be given here."
        ),
        input_model=PresetActivateInput,
        risk=Risk.MEDIUM,
        side_effects=True,
        local=True,
        handler=handler,
        targets=lambda payload: str(payload.get("preset") or ""),
    )


def _build_run_action(settings: Settings) -> ToolSpec:
    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        action_id = str(payload["action"])
        action = next((a for a in settings().actions if a.id == action_id), None)
        if action is None:
            return {"ok": False, "error": f"there is no action named {action_id!r}"}
        result = await run_action(action)
        return result.as_dict()

    return ToolSpec(
        name="presets.run_action",
        description=(
            "Start one program the user registered as an action. The command line comes from the "
            "configuration; only the action name is given here."
        ),
        input_model=RunActionInput,
        risk=Risk.HIGH,
        side_effects=True,
        local=True,
        handler=handler,
        targets=lambda payload: str(payload.get("action") or ""),
    )


def register_preset_tools(registry: ToolRegistry, runner: PresetRunner, settings: Settings) -> None:
    """Register the three `presets.*` tools."""
    for spec in (_build_list(settings), _build_activate(runner), _build_run_action(settings)):
        registry.register(spec)
    log.info("presets.tools_registered", count=3)

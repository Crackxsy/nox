"""The `presets.*` requests the dashboard calls.

Three of them, and none writes anything: presets are edited through `config.set` like every other
setting, which is what gives them the same validation against `NoxConfig`, the same audit entry
and the same live apply. A second write path here would mean a second place where a preset could
be accepted that the configuration itself rejects.

Unlike the `presets.list` *tool*, this listing includes each action's command line. The difference
is who is asking: the dashboard shows the user their own configuration, while a language model has
no use for a path that contains the user's account name.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from nox.core.config.presets import ID_PATTERN, PresetsConfig
from nox.core.logging import get_logger
from nox.ipc.dispatch import EmptyPayload, RequestContext, RequestRegistry
from nox.ipc.errors import ERR_NOT_FOUND, IpcError
from nox.presets.actions import run_action
from nox.presets.engine import PresetRunner

log = get_logger(__name__)

__all__ = ["register_preset_ipc"]

#: The two user-facing clients, never a plugin or a worker.
UI_ROLES = ("shell", "dashboard")

Settings = Callable[[], PresetsConfig]


class PresetActivateInput(BaseModel):
    """`presets.activate {preset}` - the Run button on a preset card."""

    preset: str = Field(pattern=ID_PATTERN)


class ActionTestInput(BaseModel):
    """`presets.test_action {action}` - the Test button next to a registered program."""

    action: str = Field(pattern=ID_PATTERN)


def register_preset_ipc(
    registry: RequestRegistry, runner: PresetRunner, settings: Settings
) -> None:
    """Register the three `presets.*` requests for the `shell` and `dashboard` roles."""

    async def p_list(_ctx: RequestContext, _payload: EmptyPayload) -> dict[str, Any]:
        config = settings()
        return {
            "enabled": config.enabled,
            "presets": [preset.model_dump() for preset in config.items],
            "actions": [action.model_dump() for action in config.actions],
        }

    async def p_activate(_ctx: RequestContext, payload: PresetActivateInput) -> dict[str, Any]:
        try:
            run = await runner.activate(payload.preset, trigger="manual")
        except KeyError as exc:
            raise IpcError(ERR_NOT_FOUND, f"there is no preset named {payload.preset!r}") from exc
        return run.as_dict()

    async def p_test_action(_ctx: RequestContext, payload: ActionTestInput) -> dict[str, Any]:
        """Run one registered program on its own, so a new entry can be checked before use.

        This is the same starter a `run` step uses, which is the point: a test that took a
        different path could pass while the real thing fails.
        """
        action = next((a for a in settings().actions if a.id == payload.action), None)
        if action is None:
            raise IpcError(ERR_NOT_FOUND, f"there is no action named {payload.action!r}")
        result = await run_action(action)
        log.info("presets.action_tested", action=action.id, ok=result.ok)
        return result.as_dict()

    registry.register("presets.list", EmptyPayload, p_list, roles=UI_ROLES)
    registry.register("presets.activate", PresetActivateInput, p_activate, roles=UI_ROLES)
    registry.register("presets.test_action", ActionTestInput, p_test_action, roles=UI_ROLES)

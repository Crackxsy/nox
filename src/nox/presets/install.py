"""`install(core) -> PresetsRuntime`: wires presets onto a started core.

This is where the four things a preset step can reach are connected to the four parts of the core
that actually do them - home tools through the tool executor, the assistant mode through the state
manager, speech through the speaker, and registered programs through the tool executor as well.

The order matters in one place: the core builds its orchestrator before it installs extensions, so
the voice gate is attached here rather than passed into the orchestrator's constructor - the same
way `nox.memory.install` attaches the context provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from nox.core.config import NoxConfig
from nox.core.events import E, Event
from nox.core.logging import get_logger
from nox.core.state import Mode
from nox.presets.engine import PresetRunner
from nox.presets.gate import VoicePresetGate
from nox.presets.ipc import register_preset_ipc
from nox.presets.tools import register_preset_tools
from nox.presets.triggers import PresetTriggers
from nox.tools.executor import ToolExecutor
from nox.tools.registry import ToolRegistry
from nox.voice.base import TtsRequest

log = get_logger(__name__)

__all__ = ["PresetsRuntime", "install"]

#: Presets act for the user, not for a plugin, so their tool calls carry the companion agent -
#: the same one the dashboard uses when a person presses a button.
_AGENT = "companion"

DEFAULT_MODE = Mode.COMPANION.value


class CoreLike(Protocol):
    """The subset of `NoxCore` this module needs (structural, so a test needs no real core)."""

    config: NoxConfig
    bus: Any
    state: Any
    speaker: Any
    orchestrator: Any
    tool_registry: ToolRegistry
    tool_executor: ToolExecutor
    registry: Any


@dataclass(slots=True)
class PresetsRuntime:
    """Handle for the caller: the runner other code may reuse, and the triggers to stop."""

    runner: PresetRunner
    triggers: PresetTriggers

    async def stop(self) -> None:
        await self.triggers.stop()


def _mode(core: CoreLike) -> str:
    """Current assistant mode, defaulting to `companion` so a tool call is never left modeless."""
    try:
        value = core.state.get("assistant.mode")
    except Exception as exc:  # noqa: BLE001 - never let a mode lookup break a tool call
        log.warning("presets.mode_lookup_failed", error=f"{type(exc).__name__}: {exc}")
        return DEFAULT_MODE
    return str(value) if value else DEFAULT_MODE


def install(core: CoreLike) -> PresetsRuntime:
    def settings() -> Any:
        return core.config.presets

    async def call_tool(name: str, arguments: dict[str, Any]) -> Any:
        """Every step that touches the outside world goes through the permission check."""
        return await core.tool_executor.call(
            agent=_AGENT, name=name, arguments=arguments, mode=_mode(core)
        )

    async def set_mode(mode: str) -> None:
        if mode not in {member.value for member in Mode}:
            known = ", ".join(sorted(member.value for member in Mode))
            raise ValueError(f"unknown mode {mode!r}; known modes: {known}")
        previous = _mode(core)
        if previous == mode:
            return
        await core.state.update("assistant.mode", mode, reason="preset")
        await core.bus.publish(
            Event(
                name=E.SYSTEM_MODE_CHANGED,
                payload={"previous": previous, "current": mode, "reason": "preset"},
            )
        )

    async def say(text: str) -> None:
        if core.speaker is None:
            log.info("presets.not_spoken", reason="no speaker", text_length=len(text))
            return
        await core.speaker.say(
            TtsRequest(
                utterance_id=uuid4().hex,
                text=text,
                language=core.config.identity.ui_language,
            )
        )

    runner = PresetRunner(settings=settings, call_tool=call_tool, set_mode=set_mode, say=say)

    register_preset_tools(core.tool_registry, runner, settings)
    register_preset_ipc(core.registry, runner, settings)

    if core.orchestrator is not None:
        core.orchestrator.preset_gate = VoicePresetGate(runner)
    else:
        log.warning("presets.no_orchestrator", detail="spoken phrases will not reach presets")

    triggers = PresetTriggers(runner=runner, bus=core.bus, settings=settings)
    triggers.start()

    config = core.config.presets
    log.info(
        "presets.installed",
        enabled=config.enabled,
        presets=len(config.items),
        actions=len(config.actions),
    )
    return PresetsRuntime(runner=runner, triggers=triggers)

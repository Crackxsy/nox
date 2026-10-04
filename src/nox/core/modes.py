"""Switching the assistant mode, and the security profile that belongs to it, in one place.

A mode is what Nox is doing (`stream`, `coding`, ...); the profile is what it is allowed to do while
doing it - which hosts it may reach, which plugins may run. They must change together. There used
to be two switches: the dashboard's changed both, a preset's changed only the mode, so a preset that
switched to `stream` left the companion profile in force and Twitch and OBS could never start.
Every caller - the dashboard, presets, a spoken "lass uns streamen" - goes through
:func:`switch_mode` now.
"""

from __future__ import annotations

from typing import Any, Protocol

from nox.core.events import E, Event
from nox.core.logging import get_logger
from nox.core.state import Mode

log = get_logger(__name__)

__all__ = ["DEFAULT_PROFILE", "PROFILE_FOR_MODE", "profile_for", "switch_mode"]

#: The profile each mode runs under. A mode not listed here runs under `DEFAULT_PROFILE`.
PROFILE_FOR_MODE: dict[Mode, str] = {
    Mode.CODING: "coding",
    Mode.STREAM: "stream",
    Mode.RESEARCH: "research",
}
DEFAULT_PROFILE = "companion"


class _State(Protocol):
    def get(self, path: str) -> Any: ...
    async def update(self, path: str, value: Any, *, reason: str) -> Any: ...


class _Engine(Protocol):
    def set_profile(self, profile_id: str, *, by: str) -> None: ...
    def active_profile(self) -> Any: ...


class _Bus(Protocol):
    async def publish(self, event: Event) -> None: ...


def profile_for(mode: Mode) -> str:
    return PROFILE_FOR_MODE.get(mode, DEFAULT_PROFILE)


async def switch_mode(state: _State, engine: _Engine, bus: _Bus, mode: Mode, *, by: str) -> str:
    """Set the mode and its profile, then announce it; returns the profile actually in force.

    The profile is set before the announcement, because the announcement is what the plugin
    manager reacts to: it starts the plugins the new profile allows and stops the rest, reading
    the profile at that moment. The returned id is what the engine reports, not what was asked
    for - a profile that could not be loaded is not claimed as switched.
    """
    previous = str(state.get("assistant.mode"))
    await state.update("assistant.mode", mode.value, reason=f"mode switch by {by}")
    profile = profile_for(mode)
    try:
        engine.set_profile(profile, by=by)
    except (KeyError, ValueError) as exc:
        log.error("security.profile_switch_failed", profile=profile, error=str(exc))
    await bus.publish(
        Event(
            name=E.SYSTEM_MODE_CHANGED,
            payload={"previous": previous, "current": mode.value, "reason": by},
        )
    )
    return str(engine.active_profile().id)

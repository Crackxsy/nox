"""Maps the `creative` plugin's `creative.app_detected`/`creative.app_left` events onto the normal
mode-change path.

The plugin worker cannot call the `mode.set` IPC request itself - that request is registered for
the `shell` and `dashboard` roles only and a `plugin`-role connection is refused - so this core-
side service mirrors exactly what the IPC handler does (`assistant.mode` write plus a
`system.mode_changed` publish) in reaction to the plugin's own events instead.

Two guards the IPC handler does not need, because that one is a direct user action:

- Work profile active -> the switch is suppressed entirely, so creative-app detection never mixes
  work sessions with private projects. The plugin itself cannot make this check (it has no
  profile visibility), so `creative.app_detected` still fires under the Work profile; only the
  mode switch is suppressed here.
- `assistant.mode_locked` -> respected as-is; this service never overrides a locked mode.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any, Protocol

from nox.core.events import E, Event
from nox.core.logging import get_logger
from nox.core.state import Mode

log = get_logger(__name__)

#: `Profile.id` under which creative-app detection and mode switching stay inactive.
WORK_PROFILE_ID = "work"


class _EngineLike(Protocol):
    def active_profile(self) -> Any: ...


class _SecurityLike(Protocol):
    engine: _EngineLike


class _StateLike(Protocol):
    def get(self, path: str) -> Any: ...
    async def update(self, path: str, value: Any, *, reason: str = "") -> int: ...


class _BusLike(Protocol):
    def subscribe(self, pattern: str, handler: Callable[[Event], Any]) -> Callable[[], None]: ...
    async def publish(self, event: Event) -> None: ...


class _CoreLike(Protocol):
    bus: _BusLike
    state: _StateLike
    security: _SecurityLike


class CreativeModeService:
    def __init__(self, core: _CoreLike) -> None:
        self._core = core
        self._unsub: list[Callable[[], None]] = []

    def start(self) -> None:
        self._unsub.append(self._core.bus.subscribe(E.CREATIVE_APP_DETECTED, self._on_detected))
        self._unsub.append(self._core.bus.subscribe(E.CREATIVE_APP_LEFT, self._on_left))

    def stop(self) -> None:
        for unsub in self._unsub:
            with contextlib.suppress(Exception):
                unsub()
        self._unsub.clear()

    def _work_profile_blocks_switch(self) -> bool:
        """True when the mode switch must be suppressed.

        A profile lookup that raises counts as blocking: without a readable security engine we
        cannot know that the Work profile is inactive, and switching anyway would be the unsafe
        half of the guess.
        """
        try:
            return str(self._core.security.engine.active_profile().id) == WORK_PROFILE_ID
        except Exception as exc:  # noqa: BLE001 - any failure suppresses the switch
            log.warning("creative.profile_lookup_failed", error=str(exc), exc_info=True)
            return True

    async def _on_detected(self, ev: Event) -> None:
        if self._work_profile_blocks_switch():
            log.debug("creative.mode_suppressed_work_profile", app=ev.payload.get("app"))
            return
        if bool(self._core.state.get("assistant.mode_locked")):
            return
        previous = str(self._core.state.get("assistant.mode"))
        if previous == Mode.CREATIVE.value:
            return
        await self._set_mode(previous, Mode.CREATIVE.value)

    async def _on_left(self, _ev: Event) -> None:
        if bool(self._core.state.get("assistant.mode_locked")):
            return
        previous = str(self._core.state.get("assistant.mode"))
        if previous != Mode.CREATIVE.value:
            return
        await self._set_mode(previous, Mode.COMPANION.value)

    async def _set_mode(self, previous: str, current: str) -> None:
        await self._core.state.update("assistant.mode", current, reason="creative")
        await self._core.bus.publish(
            Event(
                name=E.SYSTEM_MODE_CHANGED,
                payload={"previous": previous, "current": current, "reason": "creative"},
            )
        )

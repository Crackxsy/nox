"""Maps the `creative` plugin's `creative.app_detected`/`creative.app_left` events onto the normal
mode-change path (Spec v0.7 Creative Apps §3.1 step 3/4, ST-16-01).

The plugin worker cannot call the `mode.set` IPC request itself (`NoxCore._h_mode_set` is
registered with `roles=("shell", "dashboard")` only - a `plugin`-role connection is refused), and
`src/nox/app.py` may not be edited by this task, so this core-side service mirrors exactly what
`_h_mode_set` does (`assistant.mode` write + `system.mode_changed` publish) in reaction to the
plugin's own events instead. It is wired in by `nox.creative.install.install`, not by `app.py`.

Two guards `_h_mode_set` does not need because it is a direct user/UI action:
- Work profile active -> the switch is suppressed entirely (Spec v0.7 §5 "no mixing with private
  projects"). The plugin itself cannot make this check (no profile visibility in `PluginApi`), so
  `creative.app_detected` still fires from the plugin even under Work profile - only the mode
  switch is suppressed here. Flagged as an Open Point in the implementing agent's report.
- `assistant.mode_locked` -> respected as-is (the existing, unchanged arbitration signal); this
  service never overrides a locked mode.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any, Protocol

from nox.core.events import E, Event
from nox.core.logging import get_logger
from nox.core.state import Mode

log = get_logger(__name__)

#: `Profile.id` under which creative-app detection and mode switching stay inactive (Spec v0.7 §5).
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

    def _work_profile_active(self) -> bool:
        try:
            return str(self._core.security.engine.active_profile().id) == WORK_PROFILE_ID
        except Exception:  # profile lookup must never break detection handling
            log.debug("creative.profile_lookup_failed", exc_info=True)
            return False

    async def _on_detected(self, ev: Event) -> None:
        if self._work_profile_active():
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

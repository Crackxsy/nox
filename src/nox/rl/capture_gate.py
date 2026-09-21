"""Shared privacy-capture gate for the RL screen-capture loops (Security Model FR-7.x).

Both `plugins/rl/src/nox_plugin_rl/plugin.py`'s `_recognize_loop` (Stage 1, HUD template matching)
and `_vision_loop` (Stage 2) sample the screen/HUD region on a timer. Stage 2 already stops
sampling while a privacy zone is active or `privacy.capture.screen` is off
(`RlPlugin._on_capture_changed` / `_capture_allowed`, comment); Stage 1 had no equivalent gate - a
pre-existing gap the Vision Stage 2 story flagged rather than fixed (out of its scope). asks for
that gap to be closed *by reusing Stage 2's gate*, not by giving Stage 1 its own, separately-
behaving privacy check.

`CaptureGate` is that one, shared gate, extracted so a loop no longer has to hand-roll its own
"boolean flag + `_on_capture_changed` handler" pair (what Stage 2 currently does inline). Driven
purely by the `privacy.capture_changed` event payload (`nox.core.events.CaptureChanged`:
`microphone`/`camera`/`screen`/`cloud` booleans) `PrivacyService.effective_capture` already
publishes on every zone/mode/capture-flag/panic/safe-mode change (`nox/security/privacy.py`) -
`screen` there already ANDs together "no active privacy zone", "`privacy.capture.screen` is true",
"not panicked" and "not in safe mode" (`PrivacyService.allows_capture`), so a single boolean is
enough to satisfy FR-7.x's "zone active, or PRIVATE/OFFLINE-with-capture-off, or
`privacy.capture.screen` false" condition correctly - this gate does not need to re-derive privacy
state itself, only react to it. `on_zone_changed` is optional and only sharpens the logged pause
*reason* (naming the zone instead of a generic fallback) when a caller also forwards
`privacy.zone_changed`.

A plugin worker has no core-side `PrivacyService` access (`nox.plugins.api.PluginApi` exposes only
events/tools/state/secrets/egress - see `nox.rl.services`'s module docstring for the identical
constraint), which is exactly why this stays event-driven rather than querying a service directly.

NOTE (open point, out of this module's scope): wiring `_recognize_loop` to actually call through
`CaptureGate.maybe_capture` - and refactoring `_vision_loop` to use it instead of its inline
`_capture_allowed` flag - happens in `plugins/rl/src/nox_plugin_rl/plugin.py`, which is not part of
this change (see the report for the exact call-site edit needed).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypeVar

from nox.core.logging import get_logger

log = get_logger(__name__)

T = TypeVar("T")

#: Used when a pause has no more specific cause available (no `on_zone_changed` call preceded it).
_GENERIC_PAUSE_REASON = "privacy.capture.screen_off"


class LoggerLike(Protocol):
    def info(self, event: str, **kwargs: Any) -> None: ...


class CaptureGate:
    """Tracks whether screen capture is currently allowed and gates a capture call on it.

    `initially_allowed` defaults to `True` to match Stage 2's existing `_capture_allowed = True`
    starting value (`RlPlugin.__init__`) - a loop built on this gate behaves like Stage 2 already
    does until the first `privacy.capture_changed` event narrows it down, rather than changing
    Stage 2's startup behaviour as a side effect of reuse.
    """

    def __init__(self, *, initially_allowed: bool = True, log: LoggerLike | None = None) -> None:
        self._log: LoggerLike = log if log is not None else globals()["log"]
        self._allowed = initially_allowed
        self._reason = "" if initially_allowed else _GENERIC_PAUSE_REASON
        self._active_zone: str | None = None
        self._latched = False
        self._latch_reason = ""

    @property
    def allowed(self) -> bool:
        return self._allowed and not self._latched

    @property
    def latched(self) -> bool:
        """True once `latch` closed the gate; only `resume` opens it again."""
        return self._latched

    def latch(self, reason: str) -> None:
        """Close the gate until someone explicitly resumes it.

        This is the kill-switch and panic path: a privacy event that happens to arrive afterwards
        must not re-open capture, and neither must the next time the game is detected. Consent to
        capture is given again by a person, not by a state transition.
        """
        if self._latched:
            return
        self._latched = True
        self._latch_reason = reason
        self._log.info("rl.capture_latched", reason=reason)

    def resume(self) -> None:
        """Release a `latch`. The privacy state still decides whether capture actually runs."""
        if not self._latched:
            return
        self._latched = False
        self._latch_reason = ""
        self._log.info("rl.capture_latch_released")

    @property
    def reason(self) -> str:
        """Why capture is currently paused; `""` while `allowed` is True."""
        if self._latched:
            return self._latch_reason
        return self._reason

    def on_zone_changed(self, payload: dict[str, Any]) -> None:
        """Optional: `privacy.zone_changed` (`{"active": bool, "zone": str | None}`,
        `PrivacyService.observe_foreground`). Only sharpens the pause `reason` this gate logs -
        `on_capture_changed` alone is enough to gate capture correctly (see module docstring)."""
        self._active_zone = payload.get("zone") if payload.get("active") else None

    def on_capture_changed(self, payload: dict[str, Any]) -> None:
        """`privacy.capture_changed` handler (`CaptureChanged`). `screen` is the only field this
        gate looks at - microphone/camera/cloud are other capture kinds this loop never touches."""
        self._set(bool(payload.get("screen", True)))

    def _set(self, allowed: bool) -> None:
        if allowed == self._allowed:
            return  # no transition -> no log line (must log exactly once per transition)
        self._allowed = allowed
        if allowed:
            self._reason = ""
            self._log.info("rl.capture_resumed")
        else:
            self._reason = (
                f"zone:{self._active_zone}" if self._active_zone else _GENERIC_PAUSE_REASON
            )
            self._log.info("rl.capture_paused", reason=self._reason)

    async def maybe_capture(self, capture_fn: Callable[[], Awaitable[T]]) -> T | None:
        """Runs `capture_fn` only while the gate is open; returns `None` (no frame grabbed, not a
        discarded one - core requirement) while paused. Never suppresses `capture_fn`'s own
        exceptions - unchanged from how `_recognize_loop`/`_vision_loop` already handle a capture
        failure (`rl.capture_failed`/`rl.vision.capture_failed`, logged by the caller)."""
        if not self.allowed:
            return None
        return await capture_fn()


__all__ = ["CaptureGate"]

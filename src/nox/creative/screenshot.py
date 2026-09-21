"""Consent-gated creative-app screenshot capture, decided and performed core-side.

The `creative` plugin worker has no visibility into privacy zones or the Work profile - the plugin
API exposes the current privacy mode but never the zone definitions - so the worker only emits
`creative.screenshot.requested` and this service decides:

- Work profile active, or the profile cannot be read at all -> refused.
- privacy mode not in {FULL, BALANCED} -> refused; PRIVATE and OFFLINE never capture the screen.
- the app's window is inside an active privacy zone -> refused.
- `PrivacyService.allows_capture("screen")` false (kill switch/safe mode/panic/zone) -> refused.
- optional `mss` package not installed -> honest `unavailable`. This is the only sanctioned
  capture path; there is deliberately no second implementation to silently fall back to.
- otherwise: exactly one screenshot, saved under `paths.runtime_dir/creative/screenshots/`.

This module owns the decision and the capture. It does not own the plugin side of the exchange and
never captures without going through the checks above.
"""

from __future__ import annotations

import contextlib
import importlib.util
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from nox.core.events import E, Event
from nox.core.logging import get_logger
from nox.core.state import PrivacyMode

log = get_logger(__name__)

WORK_PROFILE_ID = "work"


def mss_available() -> bool:
    with contextlib.suppress(ImportError, ValueError):
        return importlib.util.find_spec("mss") is not None
    return False


class _EngineLike(Protocol):
    def active_profile(self) -> Any: ...


class _PrivacyLike(Protocol):
    @property
    def mode(self) -> PrivacyMode: ...
    def zone_active(self, window_title: str = "", process_name: str = "") -> bool: ...
    def allows_capture(self, kind: str) -> bool: ...


class _SecurityLike(Protocol):
    engine: _EngineLike
    privacy: _PrivacyLike


class _PathsLike(Protocol):
    runtime_dir: Path


class _ConfigLike(Protocol):
    paths: _PathsLike


class _BusLike(Protocol):
    def subscribe(self, pattern: str, handler: Callable[[Event], Any]) -> Callable[[], None]: ...
    async def publish(self, event: Event) -> None: ...


class _CoreLike(Protocol):
    bus: _BusLike
    security: _SecurityLike
    config: _ConfigLike


class CreativeScreenshotService:
    def __init__(self, core: _CoreLike) -> None:
        self._core = core
        self._unsub: Callable[[], None] | None = None

    def start(self) -> None:
        self._unsub = self._core.bus.subscribe(E.CREATIVE_SCREENSHOT_REQUESTED, self._on_request)

    def stop(self) -> None:
        if self._unsub is not None:
            with contextlib.suppress(Exception):
                self._unsub()
            self._unsub = None

    def _profile_refusal(self) -> tuple[str, str] | None:
        """Refusal for the security-profile gate, or `None` when the gate permits a capture.

        A profile lookup that raises is a refusal, not a permission: an unreadable security engine
        cannot tell us the Work profile is inactive, so the capture is denied.
        """
        try:
            active = str(self._core.security.engine.active_profile().id)
        except Exception as exc:  # noqa: BLE001 - any failure denies, and the reason is reported
            log.warning("creative.profile_lookup_failed", error=str(exc), exc_info=True)
            return (
                "unavailable",
                "the active security profile could not be read, so screenshots stay disabled "
                f"({type(exc).__name__})",
            )
        if active == WORK_PROFILE_ID:
            return "refused", "Work profile is active: creative screenshots are disabled"
        return None

    async def _on_request(self, ev: Event) -> None:
        corr = str(ev.payload.get("corr", ""))
        app = str(ev.payload.get("app", ""))
        title = str(ev.payload.get("window_title", ""))
        status, reason, path = self.decide_and_capture(app, title)
        await self._core.bus.publish(
            Event(
                name=E.CREATIVE_SCREENSHOT_RESULT,
                payload={"corr": corr, "status": status, "reason": reason, "path": path},
            )
        )

    def decide_and_capture(self, app: str, title: str) -> tuple[str, str, str]:
        """Pure decision + capture, split out from the event handler so it is directly unit
        testable without an event bus."""
        privacy = self._core.security.privacy
        profile_refusal = self._profile_refusal()
        if profile_refusal is not None:
            status, reason = profile_refusal
            return status, reason, ""
        if privacy.mode not in (PrivacyMode.FULL, PrivacyMode.BALANCED):
            return (
                "refused",
                f"privacy mode {privacy.mode.value!r} does not allow screenshots "
                "(FULL/BALANCED only)",
                "",
            )
        if privacy.zone_active(window_title=title, process_name=app):
            return "refused", "the creative app's window is inside an active privacy zone", ""
        if not privacy.allows_capture("screen"):
            return (
                "refused",
                "screen capture is currently disabled (kill switch/panic/safe mode)",
                "",
            )
        if not mss_available():
            return (
                "unavailable",
                "screenshot capture requires the optional 'mss' package, which is not installed",
                "",
            )
        try:
            path = self._capture(app)
        except Exception as exc:  # noqa: BLE001 - capture must never crash the core
            log.warning("creative.screenshot_capture_failed", error=str(exc))
            return "unavailable", f"capture failed: {exc}", ""
        return "captured", "", path

    def _capture(self, app: str) -> str:
        import mss  # local import: optional dependency, only reached once mss_available() is true
        import mss.tools

        out_dir = Path(self._core.config.paths.runtime_dir) / "creative" / "screenshots"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{app or 'window'}-{int(time.time())}.png"
        with mss.mss() as sct:
            monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            shot = sct.grab(monitor)
            mss.tools.to_png(shot.rgb, shot.size, output=str(out_path))
        return str(out_path)

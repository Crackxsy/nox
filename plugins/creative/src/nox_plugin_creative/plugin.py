"""Creative-apps plugin: notices which creative application the user is working in.

Wires `sensor.foreground_changed` through
`app_detection.match_app_family` + `HysteresisDetector` to `creative.app_detected`/
`creative.app_left`. The actual `AssistantState.mode` switch and the consent-gated screenshot
decision both happen core-side (`src/nox/creative/`, installed via `src/nox/creative/install.py`)
because this plugin worker has neither IPC role to call `mode.set` directly nor visibility into
privacy zones or the Work profile - it only emits the events those core-side services react to.

Two tools: `creative.artifact.inspect` (local metadata-only, `artifact.py`) and
`creative.screenshot.analyze` (medium/confirm; round-trips through `creative.screenshot.requested`
-> core decides -> `creative.screenshot.result`, honest `unavailable` on timeout so a missing
core-side install never hangs a tool call).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from pydantic import BaseModel, Field

from nox.plugins.api import PluginApi
from nox.security.model import Risk

from .app_detection import HysteresisDetector, match_app_family
from .artifact import inspect_artifact

#: How long a `creative.screenshot.analyze` call waits for the core-side decision before
#: reporting an honest "unavailable" (e.g. `nox.creative.install.install` was never called).
SCREENSHOT_RESULT_TIMEOUT_S = 10.0


class EmptyInput(BaseModel):
    """Tools that take no arguments."""


class ArtifactInspectInput(BaseModel):
    path: str = Field(..., min_length=1, max_length=1000)


class CreativePlugin:
    def __init__(self, api: PluginApi) -> None:
        self.api = api
        raw_patterns = api.config.get("app_patterns") or {}
        self.patterns: dict[str, list[dict[str, Any]]] = {
            family: list(entries) for family, entries in raw_patterns.items()
        }
        window_s = float(api.config.get("hysteresis_s", 15.0))
        self.detector = HysteresisDetector(window_s)
        self._timer_handle: asyncio.TimerHandle | None = None
        self._transition_tasks: set[asyncio.Task[None]] = set()
        self._last_process = ""
        self._last_title = ""
        self._pending_screenshots: dict[str, asyncio.Future[dict[str, Any]]] = {}

    # -- lifecycle ---------------------------------------------------------------------------

    async def start(self) -> None:
        self.api.events.on("sensor.foreground_changed", self._on_foreground)
        self.api.events.on("creative.screenshot.result", self._on_screenshot_result)

    async def stop(self) -> None:
        if self._timer_handle is not None:
            self._timer_handle.cancel()
            self._timer_handle = None
        for task in list(self._transition_tasks):
            task.cancel()
        self._transition_tasks.clear()
        for fut in self._pending_screenshots.values():
            if not fut.done():
                fut.cancel()
        self._pending_screenshots.clear()

    # -- detection -----------------------------------------------------------------------------

    async def _on_foreground(self, _name: str, payload: dict[str, Any]) -> None:
        process = str(payload.get("process", ""))
        title = str(payload.get("title", ""))
        self._last_process, self._last_title = process, title
        family = match_app_family(process, title, self.patterns)
        result = self.detector.sample(family)
        self._reschedule_timer()
        if result:
            await self._apply_transition(result, title)

    def _reschedule_timer(self) -> None:
        if self._timer_handle is not None:
            self._timer_handle.cancel()
            self._timer_handle = None
        remaining = self.detector.seconds_until_resolve()
        if remaining is None:
            return
        loop = asyncio.get_event_loop()
        self._timer_handle = loop.call_later(remaining + 0.01, self._on_timer)

    def _on_timer(self) -> None:
        """The debounce timer fired: the app change is settled and can be announced.

        `call_later` cannot await, so the transition runs as a task whose handle is kept and whose
        failure is logged - a dropped handle would let the task be garbage-collected mid-flight
        and swallow its exception.
        """
        self._timer_handle = None
        result = self.detector.resolve()
        if not result:
            return
        task = asyncio.ensure_future(self._apply_transition(result, self._last_title))
        self._transition_tasks.add(task)
        task.add_done_callback(self._transition_tasks.discard)
        task.add_done_callback(self._log_transition_failure)

    def _log_transition_failure(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.api.log.info("creative.transition_failed", error=f"{type(exc).__name__}: {exc}")

    async def _apply_transition(self, result: str, title: str) -> None:
        kind, _, app = result.partition(":")
        if kind == "enter":
            await self.api.events.emit("creative.app_detected", {"app": app, "window_title": title})
        else:
            await self.api.events.emit("creative.app_left", {"app": app})

    # -- screenshot ----------------------------------------------------------------------------

    async def _on_screenshot_result(self, _name: str, payload: dict[str, Any]) -> None:
        corr = str(payload.get("corr", ""))
        fut = self._pending_screenshots.pop(corr, None)
        if fut is not None and not fut.done():
            fut.set_result(dict(payload))

    async def screenshot_analyze(self, _data: EmptyInput) -> dict[str, Any]:
        """`creative.screenshot.analyze`: medium/confirm. The plugin cannot decide PRIVATE/OFFLINE,
        privacy-zone or Work-profile gating itself (no such visibility in `PluginApi`), so it asks
        the core-side `nox.creative.screenshot.CreativeScreenshotService` and relays its decision
        verbatim - never assumes success."""
        if self.detector.current is None:
            return {
                "status": "refused",
                "reason": "no creative app is currently detected in the foreground",
                "path": "",
            }
        corr = uuid.uuid4().hex
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_screenshots[corr] = fut
        await self.api.events.emit(
            "creative.screenshot.requested",
            {"corr": corr, "app": self.detector.current, "window_title": self._last_title},
        )
        try:
            result = await asyncio.wait_for(fut, timeout=SCREENSHOT_RESULT_TIMEOUT_S)
        except TimeoutError:
            self._pending_screenshots.pop(corr, None)
            return {
                "status": "unavailable",
                "reason": (
                    "no response from the core-side creative screenshot service within "
                    f"{SCREENSHOT_RESULT_TIMEOUT_S:.0f}s (nox.creative.install not wired in?)"
                ),
                "path": "",
            }
        return result

    # -- artefact --------------------------------------------------------------------------------

    async def artifact_inspect(self, data: ArtifactInspectInput) -> dict[str, Any]:
        """`creative.artifact.inspect`: local metadata-only, never touches the network."""
        return inspect_artifact(data.path)


def create(api: PluginApi) -> CreativePlugin:
    plugin = CreativePlugin(api)
    api.tools.register(
        "creative.artifact.inspect",
        ArtifactInspectInput,
        plugin.artifact_inspect,
        Risk.LOW,
        description="Local metadata-only inspection of an audio/video/image/.blend artefact.",
        side_effects=False,
        local=True,
    )
    api.tools.register(
        "creative.screenshot.analyze",
        EmptyInput,
        plugin.screenshot_analyze,
        Risk.MEDIUM,
        description=(
            "One consent-gated screenshot of the currently detected creative app's window "
            "(FULL/BALANCED only, never in a privacy zone or under the Work profile)."
        ),
        side_effects=True,
        local=True,
    )
    return plugin

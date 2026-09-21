"""Wiring entry point for the core-side half of the `creative` plugin.

`install(core)` starts the mode bridge and the screenshot gate and returns the runtime that owns
them; stopping the runtime detaches both from the bus. Everything here is additive: without the
`creative` plugin running, both services simply never see an event.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .screenshot import CreativeScreenshotService
from .service import CreativeModeService


@dataclass(slots=True)
class CreativeRuntime:
    """The two core-side services; `stop` unsubscribes them."""

    mode: CreativeModeService
    screenshot: CreativeScreenshotService

    def stop(self) -> None:
        self.screenshot.stop()
        self.mode.stop()


def install(core: Any) -> CreativeRuntime:
    runtime = CreativeRuntime(
        mode=CreativeModeService(core), screenshot=CreativeScreenshotService(core)
    )
    runtime.mode.start()
    runtime.screenshot.start()
    return runtime


__all__ = ["CreativeRuntime", "install"]

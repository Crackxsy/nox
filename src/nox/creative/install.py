"""Wiring entry point for the `creative` plugin's core-side services (Spec v0.7 Creative Apps,
EPIC-16). `src/nox/app.py` may not be edited by this task (ENGINEERING.md shared-file rule /
coordination note); `install(core)` is additive-only (uses only the already-public
`core.bus`/`core.state`/`core.security`/`core.config` attributes NoxCore already exposes) and is
meant to be called once by the integrator from `NoxCore.start()` - or directly by tests/whatever
wires plugin-adjacent core services together. Calling it is a no-op if the `creative` plugin is
never started (`config.plugins.enabled` does not include `"creative"` by default)."""

from __future__ import annotations

from typing import Any

from .screenshot import CreativeScreenshotService
from .service import CreativeModeService


def install(core: Any) -> None:
    mode_service = CreativeModeService(core)
    screenshot_service = CreativeScreenshotService(core)
    mode_service.start()
    screenshot_service.start()
    # Kept on `core` so they are not garbage-collected and can be stopped on shutdown; not read by
    # any other module, so this does not widen NoxCore's public contract.
    core.creative_services = (mode_service, screenshot_service)

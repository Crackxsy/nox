"""`install(core)`: wires the Clip Pipeline onto a booted `NoxCore` (ST-15-01..06, Spec v0.6 Clip
Pipeline). Not called from `nox.app` yet - per ENGINEERING.md's shared-file rule, `src/nox/app.py`
is not edited by this change; the integrator adds one `from nox.clips.install import install` +
`self.clips = install(self)` call (mirroring the "10b. Stream Bot core services" block, after
`self.tool_executor`/`self.registry` exist and before `self.plugins.start()`), or wires it through
whatever composition point the integrator prefers.

Expects `core` to expose (all present once `NoxCore.start()` has run through step "7a. tool
registry + executor"): `.config: NoxConfig`, `.db: Database`, `.bus: EventBus`, `.tool_registry:
ToolRegistry`, `.tool_executor: ToolExecutor`, `.registry: RequestRegistry`, `.state` (for the
current `assistant.mode`, used as the tool-call `mode` for `clip.*` tool invocations - same
pattern as `nox.stream.booking.FunkenBooking`)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from nox.clips.ipc import register_clip_ipc
from nox.clips.repository import ClipMarkerRepository, ClipRepository
from nox.clips.service import ClipCaptureService
from nox.clips.tools import register_clip_tools
from nox.clips.trim import TrimBackend
from nox.clips.watcher import ClipWatcher
from nox.core.config import NoxConfig
from nox.core.events import EventBus
from nox.core.logging import get_logger
from nox.data.db import Database
from nox.ipc.dispatch import RequestRegistry
from nox.tools.executor import ToolExecutor
from nox.tools.registry import ToolRegistry

log = get_logger(__name__)


class CoreLike(Protocol):
    """The subset of `NoxCore` this module needs (structural, so tests can supply a minimal
    stand-in without booting the whole core)."""

    config: NoxConfig
    db: Database
    bus: EventBus
    tool_registry: ToolRegistry
    tool_executor: ToolExecutor
    registry: RequestRegistry
    state: object


@dataclass(slots=True)
class ClipsRuntime:
    """Handles the integrator (or a test) needs to stop the background pieces on shutdown."""

    repo: ClipRepository
    markers: ClipMarkerRepository
    service: ClipCaptureService
    watcher: ClipWatcher

    async def stop(self) -> None:
        await self.service.stop()
        await self.watcher.stop()


def _mode(core: CoreLike) -> str:
    try:
        value = core.state.get("assistant.mode")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - never let a mode lookup break a tool call
        return "companion"
    return str(value) if value else "companion"


def install(core: CoreLike) -> ClipsRuntime:
    cfg = core.config.clips
    repo = ClipRepository(core.db)
    markers = ClipMarkerRepository(core.db)

    service = ClipCaptureService(core.bus, core.tool_executor, repo, cfg)
    service.start()

    watcher = ClipWatcher(core.bus, repo, cfg)
    watcher.start()

    backend = TrimBackend()
    register_clip_tools(core.tool_registry, repo, cfg, core.bus, backend)
    register_clip_ipc(core.registry, core.tool_executor, lambda: _mode(core))

    log.info(
        "clips.installed",
        library_root=str(cfg.library_root),
        watch_dir=str(cfg.watch_dir),
        ffmpeg_available=backend.available(),
    )
    return ClipsRuntime(repo=repo, markers=markers, service=service, watcher=watcher)

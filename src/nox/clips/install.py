"""`install(core) -> ClipsRuntime`: wires the clip pipeline onto a started core.

Expects `core` to expose everything on `CoreLike` below, which is what the core has once its tool
registry and executor exist. `.state` supplies the current `assistant.mode`, which becomes the mode
of every `clip.*` tool call this module makes on the user's behalf.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

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
    state: Any


@dataclass(slots=True)
class ClipsRuntime:
    """Handles the caller needs to stop the background pieces on shutdown."""

    repo: ClipRepository
    markers: ClipMarkerRepository
    service: ClipCaptureService
    watcher: ClipWatcher

    async def stop(self) -> None:
        await self.service.stop()
        await self.watcher.stop()


class _StateLike(Protocol):
    def get(self, path: str) -> object: ...


def _mode(core: CoreLike) -> str:
    """Current assistant mode, defaulting to `companion` so a tool call is never left modeless."""
    state: _StateLike = core.state
    try:
        value = state.get("assistant.mode")
    except Exception as exc:  # noqa: BLE001 - never let a mode lookup break a tool call
        log.warning("clips.mode_lookup_failed", error=f"{type(exc).__name__}: {exc}")
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

"""Composition entry point for the project-management slice: `install(core)` registers the `pm.*`
tools and starts the vault watcher, and returns the runtime that owns both.

The initial vault scan (a few hundred notes, several seconds) runs in a background task off the
event loop, like the memory extension's full scan: `install` returns immediately and the `pm`
health check reports `limited` until the first scan finishes. Doing it inline blocked the loop,
stopped the core's heartbeats, and had the supervisor treat the boot as a hang.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from nox.core.events import HealthStatus
from nox.core.health import Check
from nox.core.logging import get_logger
from nox.pm.focus import FocusService
from nox.pm.index import PmIndex
from nox.pm.models import WorkItem
from nox.pm.tools import register_pm_tools
from nox.pm.vault_repo import PmVaultRepo
from nox.pm.watcher import PmWatcher

log = get_logger(__name__)


@dataclass
class PmRuntime:
    """Handles for the caller. `indexed` is set once the first scan has finished; until then the
    index is empty and the `pm.*` tools answer with what they have."""

    repo: PmVaultRepo
    index: PmIndex
    focus: FocusService
    watcher: PmWatcher
    indexed: asyncio.Event = field(default_factory=asyncio.Event)
    item_count: int = 0
    task: asyncio.Task[None] | None = None

    async def ready(self, timeout: float | None = None) -> bool:
        """Await the initial scan. Returns False when it did not finish inside `timeout`."""
        try:
            await asyncio.wait_for(self.indexed.wait(), timeout)
        except TimeoutError:
            return False
        return True

    def stop(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self.watcher.stop()


def install(core: Any) -> PmRuntime:
    """`core` is a started core, or anything exposing the same `config`, `db`, `bus`, `state` and
    `tool_registry` attributes. Returns at once; the initial scan runs in the background - await
    `PmRuntime.ready` when a populated index is needed."""
    cfg = core.config
    pm_cfg = cfg.pm
    repo = PmVaultRepo(
        cfg.paths.vault_dir,
        epics_dir=pm_cfg.epics_dir,
        stories_dir=pm_cfg.stories_dir,
        projects_note=pm_cfg.projects_note,
    )
    index = PmIndex(core.db)
    focus = FocusService(index, core.bus, limit=pm_cfg.focus_max_items)
    register_pm_tools(
        core.tool_registry, repo=repo, index=index, focus=focus, bus=core.bus, state=core.state
    )

    watcher = PmWatcher(repo, index, core.bus, focus=focus, debounce_s=pm_cfg.watch_debounce_s)
    runtime = PmRuntime(repo=repo, index=index, focus=focus, watcher=watcher)
    runtime.task = asyncio.get_running_loop().create_task(
        _initial_index(runtime), name="nox-pm-initial-index"
    )
    _add_health_check(core, runtime)
    return runtime


async def _initial_index(runtime: PmRuntime) -> None:
    """Read every work-item note and rebuild the index off the loop, then start watching."""
    try:
        items: list[WorkItem] = await asyncio.to_thread(runtime.repo.all_items)
        await asyncio.to_thread(runtime.index.rebuild, items)
    except Exception as exc:  # noqa: BLE001 - a broken vault degrades pm, it never kills the boot
        log.error("pm.index_failed", error=f"{type(exc).__name__}: {exc}")
        return
    runtime.watcher.seed_known_hashes(items)
    runtime.watcher.start()
    runtime.item_count = len(items)
    runtime.indexed.set()
    log.info("pm.indexed", count=len(items))


def _add_health_check(core: Any, runtime: PmRuntime) -> None:
    """`limited` while the first scan runs - honest, never a faked `available`."""
    health = getattr(core, "health", None)
    if health is None:
        return

    async def probe() -> tuple[HealthStatus, str]:
        if not runtime.indexed.is_set():
            return HealthStatus.LIMITED, "indexing the vault"
        return HealthStatus.AVAILABLE, f"{runtime.item_count} work items"

    try:
        health.add_check(Check("pm", probe))
    except ValueError:  # installed twice on the same core (tests)
        pass

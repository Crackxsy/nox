"""`install(core)`: watchers - their table, their loop, their tools and the dashboard's requests.

A finding is reported like a reminder the user set: `ProactiveService.notify(..., "reminder", ...)`
with `context_key="watch:<id>"`. The user asked for exactly this, so the proactive budget and the
timing learner do not hold it back; mute and quiet zones still do, and the dashboard always shows
it. The scheduler runs headless as well - on the Pi there is no voice, and the notification still
reaches the dashboard and every client that listens for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from nox.core.logging import get_logger
from nox.ipc.dispatch import EmptyPayload, RequestContext
from nox.ipc.roles import LOCAL_UI_ROLES
from nox.watch.tools import register_watch_tools, watcher_dict
from nox.watch.watchers import WatcherRepository, WatcherRow, WatcherScheduler

log = get_logger(__name__)

__all__ = ["WatchRuntime", "install"]

AGENT = "watcher"
DEFAULT_MODE = "companion"


@dataclass
class WatchRuntime:
    repo: WatcherRepository
    scheduler: WatcherScheduler

    async def stop(self) -> None:
        await self.scheduler.stop()


class WatchDelete(BaseModel):
    id: str = Field(min_length=1, max_length=40)


def _now() -> datetime:
    return datetime.now(UTC)


def _mode(core: Any) -> str:
    try:
        value = core.state.get("assistant.mode") if core.state is not None else None
    except Exception:  # noqa: BLE001 - an unreadable mode is the default mode
        return DEFAULT_MODE
    return str(value) if value else DEFAULT_MODE


def install(core: Any) -> WatchRuntime | None:
    cfg = core.config.watch
    if not cfg.enabled:
        log.info("watch.disabled")
        return None
    repo = WatcherRepository(core.db)

    async def call_tool(name: str, arguments: dict[str, Any]) -> Any:
        return await core.tool_executor.call(
            agent=AGENT, name=name, arguments=arguments, mode=_mode(core), origin="watch"
        )

    async def report(watcher: WatcherRow, value: str) -> None:
        proactive = core.extensions.get("proactive") if core.extensions else None
        text = f"{watcher.title} ({value})" if value else watcher.title
        if proactive is None:
            log.warning("watch.report_unspoken", id=watcher.id, reason="proactive not installed")
            return
        await proactive.service.notify(
            "urgent", "reminder", text, context_key=f"watch:{watcher.id}"
        )

    def deliverable() -> bool:
        proactive = core.extensions.get("proactive") if core.extensions else None
        return proactive is not None and bool(core.config.proactive.enabled)

    scheduler = WatcherScheduler(
        repo,
        call_tool=call_tool,
        lookup=core.tool_registry.get,
        report=report,
        poll_s=cfg.poll_s,
        deliverable=deliverable,
        call_timeout_s=cfg.call_timeout_s,
    )
    register_watch_tools(core.tool_registry, repo, lambda: core.config.watch, _now)

    async def list_watchers(_ctx: RequestContext, _p: EmptyPayload) -> dict[str, Any]:
        return {"watchers": [watcher_dict(row) for row in repo.list_active()]}

    async def delete(ctx: RequestContext, p: WatchDelete) -> dict[str, Any]:
        ok = repo.delete(p.id)
        if ok and core.security is not None:
            core.security.audit.append(
                actor=ctx.role,
                tool="watch",
                action="delete",
                target=p.id,
                decision="allow",
                result="ok",
            )
        log.info("watch.deleted", id=p.id, by=ctx.role, ok=ok)
        return {"ok": ok}

    if core.registry is not None:
        core.registry.register("watch.list", EmptyPayload, list_watchers, roles=LOCAL_UI_ROLES)
        core.registry.register("watch.delete", WatchDelete, delete, roles=LOCAL_UI_ROLES)
    scheduler.start()
    log.info("watch.installed", watchers=repo.count_active())
    return WatchRuntime(repo=repo, scheduler=scheduler)

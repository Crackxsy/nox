"""Dashboard/shell-facing `clip.*` IPC requests (ST-15-05/06): thin passthroughs to the `clip.*`
tools registered in `nox.clips.tools`, routed through the real `ToolExecutor` so every dashboard
action still gets the normal permission check (confirm on `clip.export`/`clip.trim`, `medium` risk)
and audit trail - not a shortcut around it."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nox.clips.tools import ClipExportInput, ClipListInput, ClipTagInput, ClipTrimInput
from nox.ipc.dispatch import RequestContext, RequestRegistry
from nox.ipc.errors import ERR_INTERNAL, ERR_PERMISSION, IpcError
from nox.tools.executor import ERR_PERMISSION_DENIED, ToolExecutor

ModeProvider = Callable[[], str]


def _raise_for(result: Any) -> None:
    code = ERR_PERMISSION if result.error == ERR_PERMISSION_DENIED else ERR_INTERNAL
    raise IpcError(code, result.error or "clip tool call failed")


def register_clip_ipc(
    registry: RequestRegistry, executor: ToolExecutor, mode: ModeProvider
) -> None:
    async def h_list(ctx: RequestContext, p: ClipListInput) -> dict[str, Any]:
        result = await executor.call(
            agent=ctx.role, name="clip.list", arguments=p.model_dump(), mode=mode()
        )
        if not result.ok:
            _raise_for(result)
        return result.data or {}

    async def h_tag(ctx: RequestContext, p: ClipTagInput) -> dict[str, Any]:
        result = await executor.call(
            agent=ctx.role, name="clip.tag", arguments=p.model_dump(), mode=mode()
        )
        if not result.ok:
            _raise_for(result)
        return result.data or {}

    async def h_export(ctx: RequestContext, p: ClipExportInput) -> dict[str, Any]:
        result = await executor.call(
            agent=ctx.role, name="clip.export", arguments=p.model_dump(), mode=mode()
        )
        if not result.ok:
            _raise_for(result)
        return result.data or {}

    async def h_trim(ctx: RequestContext, p: ClipTrimInput) -> dict[str, Any]:
        result = await executor.call(
            agent=ctx.role, name="clip.trim", arguments=p.model_dump(), mode=mode()
        )
        if not result.ok:
            _raise_for(result)
        return result.data or {}

    ui = ("shell", "dashboard")
    registry.register("clip.list", ClipListInput, h_list, roles=ui)
    registry.register("clip.tag", ClipTagInput, h_tag, roles=ui)
    registry.register("clip.export", ClipExportInput, h_export, roles=ui)
    registry.register("clip.trim", ClipTrimInput, h_trim, roles=ui)

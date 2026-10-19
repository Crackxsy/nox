"""The dashboard's "Fähigkeiten" requests for external MCP servers.

List, add, approve the first start, switch on and off, remove. Only the two user-facing clients
(`shell`, `dashboard`) may call them - never a plugin, a worker, the MCP bridge or a phone - so a
model cannot add or approve a server for itself: approving runs code, and that is the user's call.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ValidationError

from nox.core.config.hands import SERVER_ID_PATTERN, McpServerConfig
from nox.ipc.dispatch import EmptyPayload, RequestContext, RequestRegistry
from nox.mcp.servers import McpServerManager

__all__ = ["UI_ROLES", "register_mcp_client_ipc"]

UI_ROLES = ("shell", "dashboard")


class ServerAdd(BaseModel):
    id: str = Field(pattern=SERVER_ID_PATTERN)
    name: str = Field(default="", max_length=80)
    command: list[str] = Field(default_factory=list, max_length=32)
    url: str = Field(default="", max_length=500)
    version: str = Field(default="", max_length=64)
    env: dict[str, str] = Field(default_factory=dict, max_length=32)
    headers: dict[str, str] = Field(default_factory=dict, max_length=16)
    network: bool = True
    read_only_tools: list[str] = Field(default_factory=list, max_length=64)


class ServerRef(BaseModel):
    id: str = Field(pattern=SERVER_ID_PATTERN)


class ServerEnable(ServerRef):
    enabled: bool


def _first_error(exc: ValidationError) -> str:
    error = exc.errors()[0]
    where = ".".join(str(part) for part in error.get("loc", ()))
    return f"{where}: {error.get('msg', 'invalid')}" if where else str(error.get("msg", "invalid"))


def register_mcp_client_ipc(registry: RequestRegistry, manager: McpServerManager) -> None:
    async def list_servers(_ctx: RequestContext, _payload: EmptyPayload) -> dict[str, Any]:
        return {"sandboxed": manager.sandboxed, "servers": manager.status()}

    async def add(ctx: RequestContext, payload: ServerAdd) -> dict[str, Any]:
        try:
            cfg = McpServerConfig.model_validate(payload.model_dump())
        except ValidationError as exc:
            return {"ok": False, "reason": _first_error(exc)}
        return await manager.add(cfg, by=ctx.role)

    async def approve(ctx: RequestContext, payload: ServerRef) -> dict[str, Any]:
        return await manager.approve(payload.id, by=ctx.role)

    async def enable(ctx: RequestContext, payload: ServerEnable) -> dict[str, Any]:
        return await manager.set_enabled(payload.id, payload.enabled, by=ctx.role)

    async def remove(ctx: RequestContext, payload: ServerRef) -> dict[str, Any]:
        return await manager.remove(payload.id, by=ctx.role)

    registry.register("mcp.servers.list", EmptyPayload, list_servers, roles=UI_ROLES)
    registry.register("mcp.servers.add", ServerAdd, add, roles=UI_ROLES)
    registry.register("mcp.servers.approve", ServerRef, approve, roles=UI_ROLES)
    registry.register("mcp.servers.enable", ServerEnable, enable, roles=UI_ROLES)
    registry.register("mcp.servers.remove", ServerRef, remove, roles=UI_ROLES)

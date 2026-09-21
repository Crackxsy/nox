"""The `home.*` IPC requests the dashboard and the shell call.

Seven names, each a thin passthrough to a tool the `home` plugin registered, routed through the
real `ToolExecutor` so a dashboard toggle gets the same permission check and the same audit entry
a model-initiated call would - not a shortcut around them. The two exceptions are
`home.test` (a direct REST probe, because a user is testing the credentials they just typed, not
the worker's old session) and `home.command`, which runs the deterministic intent layer.

`home.command` is the reason this module exists at all: "mach das licht im wohnzimmer aus" is
resolved here, from the entity list, with string matching, and only falls back to the model when
nothing matches. The response says which of the three happened - `matched`, `refused` or neither -
and reports the measured matching latency, so the claim "no AI round trip" is visible rather than
asserted.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import BaseModel, Field

from nox.core.config import HomeConfig
from nox.core.logging import get_logger
from nox.home.intent import IntentMatch, IntentRefusal, IntentSnapshot, resolve_intent
from nox.home.probe import probe_home_assistant
from nox.ipc.dispatch import EmptyPayload, RequestContext, RequestRegistry
from nox.ipc.errors import ERR_INTERNAL, ERR_PERMISSION, ERR_UNAVAILABLE, IpcError
from nox.tools.executor import ERR_PERMISSION_DENIED, ERR_UNKNOWN_TOOL, ToolExecutor

log = get_logger(__name__)

#: The roles that may reach any of these: the two user-facing clients, never a plugin or a worker.
UI_ROLES = ("shell", "dashboard")

ModeProvider = Callable[[], str]
SecretReader = Callable[[], str | None]
ClientFactory = Callable[..., httpx.AsyncClient]
SettingsProvider = Callable[[], HomeConfig]


class HomeListInput(BaseModel):
    """`home.list {area?, domain?}`."""

    area: str = Field(default="", max_length=120)
    domain: str = Field(default="", max_length=40)


class HomeLightInput(BaseModel):
    """`home.light` - the dashboard's light toggle and brightness slider."""

    entity_ids: list[str] = Field(min_length=1, max_length=50)
    on: bool | None = None
    brightness_pct: int | None = Field(default=None, ge=0, le=100)
    color_temp_kelvin: int | None = Field(default=None, ge=1500, le=6600)


class HomeSwitchInput(BaseModel):
    """`home.switch` - the dashboard's socket toggle."""

    entity_ids: list[str] = Field(min_length=1, max_length=50)
    on: bool


class HomeSceneInput(BaseModel):
    """`home.scene` - one scene button."""

    entity_id: str = Field(min_length=3, max_length=255)


class HomeCommandInput(BaseModel):
    """`home.command {text}` - one sentence for the deterministic intent layer."""

    text: str = Field(min_length=1, max_length=400)


def _raise_for(result: Any) -> None:
    """Turn a failed tool call into the IPC error that says what a user can do about it.

    An unknown tool is the ordinary state of a fresh installation - the `home` plugin is not in
    `plugins.enabled` yet - so it becomes `unavailable` with that sentence rather than an internal
    error, and the dashboard shows its "nicht verbunden" tile instead of a developer string.
    """
    if result.error == ERR_PERMISSION_DENIED:
        raise IpcError(ERR_PERMISSION, result.error)
    if result.error == ERR_UNKNOWN_TOOL:
        raise IpcError(
            ERR_UNAVAILABLE,
            "the home plugin is not running: add 'home' to plugins.enabled and set up "
            "Home Assistant under Settings",
            retryable=True,
        )
    raise IpcError(ERR_INTERNAL, result.error or "home tool call failed")


def register_home_ipc(
    registry: RequestRegistry,
    executor: ToolExecutor,
    mode: ModeProvider,
    *,
    settings: SettingsProvider,
    token: SecretReader,
    client_factory: ClientFactory,
) -> None:
    """Register the seven `home.*` requests for the `shell` and `dashboard` roles."""

    async def call(ctx: RequestContext, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await executor.call(agent=ctx.role, name=name, arguments=arguments, mode=mode())
        if not result.ok:
            _raise_for(result)
        return result.data or {}

    async def h_status(ctx: RequestContext, _p: EmptyPayload) -> dict[str, Any]:
        return await call(ctx, "home.status.read", {})

    async def h_list(ctx: RequestContext, p: HomeListInput) -> dict[str, Any]:
        return await call(ctx, "home.list", p.model_dump())

    async def h_light(ctx: RequestContext, p: HomeLightInput) -> dict[str, Any]:
        return await call(ctx, "home.light", p.model_dump(exclude_none=True))

    async def h_switch(ctx: RequestContext, p: HomeSwitchInput) -> dict[str, Any]:
        return await call(ctx, "home.switch", p.model_dump())

    async def h_scene(ctx: RequestContext, p: HomeSceneInput) -> dict[str, Any]:
        return await call(ctx, "home.scene", p.model_dump())

    async def h_test(_ctx: RequestContext, _p: EmptyPayload) -> dict[str, Any]:
        result = await probe_home_assistant(settings(), token(), client_factory)
        return result.as_dict()

    async def h_command(ctx: RequestContext, p: HomeCommandInput) -> dict[str, Any]:
        return await run_command(ctx, p.text, call)

    registry.register("home.status", EmptyPayload, h_status, roles=UI_ROLES)
    registry.register("home.list", HomeListInput, h_list, roles=UI_ROLES)
    registry.register("home.light", HomeLightInput, h_light, roles=UI_ROLES)
    registry.register("home.switch", HomeSwitchInput, h_switch, roles=UI_ROLES)
    registry.register("home.scene", HomeSceneInput, h_scene, roles=UI_ROLES)
    registry.register("home.test", EmptyPayload, h_test, roles=UI_ROLES)
    registry.register("home.command", HomeCommandInput, h_command, roles=UI_ROLES)


ToolCaller = Callable[[RequestContext, str, dict[str, Any]], Any]


async def run_command(ctx: RequestContext, text: str, call: ToolCaller) -> dict[str, Any]:
    """Resolve one sentence deterministically and, if it resolved, execute it.

    The returned `match_ms` is the matching cost alone - the `home.list` call before it and the
    tool call after it are measured separately, because only the matching is the part that claims
    to be free of an AI round trip.
    """
    listing = await call(ctx, "home.list", {"area": "", "domain": ""})
    snapshot = IntentSnapshot.from_payload(listing)
    started = time.perf_counter()
    intent = resolve_intent(text, snapshot)
    match_ms = (time.perf_counter() - started) * 1000.0

    if isinstance(intent, IntentRefusal):
        log.warning("home.command_refused", word=intent.matched_word, reason=intent.reason)
        return {
            "matched": False,
            "refused": True,
            "reason": intent.reason,
            "tool": "",
            "summary": "",
            "match_ms": round(match_ms, 3),
            "result": {},
        }
    if not isinstance(intent, IntentMatch):
        return {
            "matched": False,
            "refused": False,
            "reason": "",
            "tool": "",
            "summary": "",
            "match_ms": round(match_ms, 3),
            "result": {},
        }
    result = await call(ctx, intent.tool, dict(intent.arguments))
    return {
        "matched": True,
        "refused": False,
        "reason": "",
        "tool": intent.tool,
        "summary": intent.summary,
        "confidence": round(intent.confidence, 3),
        "entity_ids": list(intent.entity_ids),
        "match_ms": round(match_ms, 3),
        "result": result,
    }

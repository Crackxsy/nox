"""`install(core)`: self-built skills - the service, its tools, the dashboard's requests.

Allowed in every profile (companion included): building needs no shell, only Claude Code with its
file tools on one folder. Without Claude Code, or in a privacy mode that forbids the cloud,
`skill.learn` says honestly that nothing can be learned right now.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from typing import Any

from pydantic import BaseModel, Field

from nox.capabilities.gaps import CURATED_GAPS
from nox.capabilities.model import Gap
from nox.core.events import E, Event
from nox.core.logging import get_logger
from nox.ipc.dispatch import EmptyPayload, RequestContext
from nox.ipc.roles import LOCAL_UI_ROLES
from nox.paths import nox_home
from nox.plugins.manager import ORIGIN_BUNDLED
from nox.plugins.manifest import PluginManifest, authorize_egress
from nox.settings.layers import resolve_user_config_path, write_user_config
from nox.skills.builder import ClaudeCodeSkillBuilder
from nox.skills.service import SkillService
from nox.skills.testing import NO_SANDBOX, default_test_runner
from nox.skills.tools import register_skill_tools
from nox.skills.worker_launch import sandbox_factory_for

log = get_logger(__name__)

__all__ = ["SkillsRuntime", "install"]

#: Namespaces of the core's own tool families that are not always registered (an extension may be
#: off), so a skill can never claim one of them.
CORE_NAMESPACES = frozenset(
    {"skill", "skills", "watch", "web", "browser", "shell", "package", "screen", "camera",
     "vision", "mic", "computer", "file", "files", "calendar", "reminder", "memory", "vault",
     "capabilities", "config", "plans", "presets", "extend", "time", "state", "sensors", "views"}
)  # fmt: skip


class SkillRef(BaseModel):
    id: str = Field(min_length=2, max_length=40)


class SkillActivate(SkillRef):
    pin: str | None = Field(default=None, max_length=64)


class SkillsRuntime:
    def __init__(self, service: SkillService) -> None:
        self.service = service
        self._unsubs: list[Callable[[], None]] = []

    def watch(self, core: Any) -> None:
        self._unsubs = [
            core.bus.subscribe(E.SECURITY_KILL_SWITCH, self._on_kill),
            core.bus.subscribe(E.SYSTEM_STARTED, self._on_started),
        ]

    async def _on_kill(self, _event: Event) -> None:
        await self.service.halt()

    async def _on_started(self, _event: Event) -> None:
        self.service.resume()

    async def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        await self.service.stop()


def _gap_source(core: Any) -> Callable[[], tuple[Iterable[Gap], Iterable[str]]]:
    def source() -> tuple[Iterable[Gap], Iterable[str]]:
        capabilities = core.extensions.get("capabilities") if core.extensions else None
        report = getattr(capabilities, "report", None)
        if callable(report):
            built = report()
            return built.gaps, [c.name for c in built.usable]
        return CURATED_GAPS, core.tool_registry.names()

    return source


def _runtime_unavailable(manager: Any) -> Callable[[], str | None]:
    """Why an activated skill could not run here: no sandboxed spawner, or no strong sandbox."""

    def reason() -> str | None:
        spawner = getattr(manager, "sandboxed_spawner", None)
        return NO_SANDBOX if spawner is None else spawner.unavailable()

    return reason


def install(core: Any) -> SkillsRuntime | None:
    cfg = core.config.skills
    manager = core.plugins
    if manager is None or core.tool_registry is None:
        log.warning("skills.not_installed", reason="no plugin manager")
        return None
    user_path = resolve_user_config_path()

    def taken() -> set[str]:
        names = {name.split(".", 1)[0] for name in core.tool_registry.names()}
        bundled = {rid for rid, rec in manager.records().items() if rec.origin == ORIGIN_BUNDLED}
        return names | bundled | CORE_NAMESPACES

    def new_hosts(manifest: PluginManifest) -> list[str]:
        decisions = authorize_egress(
            manifest,
            core.security.engine.active_profile(),
            global_allowlist=manager.global_egress_allowlist,
            loopback_allowlist=tuple(core.config.security.loopback_allowlist),
        )
        return [d.entry for d in decisions if not d.allowed]

    async def persist_enabled(plugin_id: str, enabled: bool) -> None:
        current = [p for p in core.config.plugins.enabled if p != plugin_id]
        if enabled:
            current.append(plugin_id)
        await asyncio.to_thread(write_user_config, user_path, {"plugins": {"enabled": current}})
        core.config.plugins.enabled = current

    async def notify(text: str, context_key: str) -> None:
        proactive = core.extensions.get("proactive") if core.extensions else None
        if proactive is not None:
            await proactive.service.notify("urgent", "task_result", text, context_key=context_key)

    def secret_present(name: str) -> bool:
        try:
            return core.security.secrets.get(name) is not None
        except Exception:  # noqa: BLE001 - an unavailable keyring is "not present"
            return False

    service = SkillService(
        root=nox_home() / "skills",
        plugins_root=nox_home() / "plugins",
        builder=ClaudeCodeSkillBuilder(
            command=core.config.ai.providers.claude_code.command,
            allows_cloud=core.security.privacy.allows_cloud,
            max_turns=cfg.max_turns,
            timeout_s=cfg.build_timeout_s,
            model=cfg.model,
        ),
        tests=default_test_runner(sandbox_factory_for(core.config)),
        config=lambda: core.config.skills,
        plugins=manager,
        taken_namespaces=taken,
        new_hosts=new_hosts,
        gate=core.security.gate,
        persist_enabled=persist_enabled,
        notify=notify,
        audit=core.security.audit,
        home_host=lambda: (
            str(core.config.home.host or "") if "home" in core.config.plugins.enabled else ""
        ),
        secret_present=secret_present,
        runtime_unavailable=_runtime_unavailable(manager),
        revoke_grants=lambda prefix: core.security.engine.revoke_scope(prefix, by="skills"),
    )
    service.start()
    register_skill_tools(core.tool_registry, service, _gap_source(core))
    hooks = getattr(core, "secret_name_hooks", None)
    if isinstance(hooks, list):
        hooks.append(service.declares_secret)

    async def list_skills(_ctx: RequestContext, _p: EmptyPayload) -> dict[str, Any]:
        return service.status()

    async def activate(ctx: RequestContext, p: SkillActivate) -> dict[str, Any]:
        return await service.activate(p.id, pin=p.pin, by=ctx.role)

    async def deactivate(ctx: RequestContext, p: SkillRef) -> dict[str, Any]:
        return await service.deactivate(p.id, by=ctx.role)

    async def forget(ctx: RequestContext, p: SkillRef) -> dict[str, Any]:
        return await service.forget(p.id, by=ctx.role)

    if core.registry is not None:
        core.registry.register("skills.list", EmptyPayload, list_skills, roles=LOCAL_UI_ROLES)
        core.registry.register("skills.activate", SkillActivate, activate, roles=LOCAL_UI_ROLES)
        core.registry.register("skills.deactivate", SkillRef, deactivate, roles=LOCAL_UI_ROLES)
        core.registry.register("skills.forget", SkillRef, forget, roles=LOCAL_UI_ROLES)
    runtime = SkillsRuntime(service)
    runtime.watch(core)
    log.info("skills.installed", available=service.builder.unavailable() is None)
    return runtime

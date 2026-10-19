"""`install(core)`: Nox's two MCP directions.

**Client** (`nox.mcp.servers`): other people's MCP servers from `mcp.servers`, as `mcp.<id>.<tool>`
tools, with the dashboard requests that add, approve and remove them. Installed whether or not
Claude Code is there - any model that calls tools can use them.

**Bridge** - connects the Claude Code conversation session to Nox's tools.

Creates the `McpGate`, registers its two IPC requests for the `mcp` role, hands it to the Claude
Code provider's conversation session, and ties that session to the kill switch: on
`security.kill_switch` the Claude Code process tree and its bridge die immediately, and nothing is
restarted until the system is resumed. When the provider is configured for it, the session is
started ahead of the first turn, so the first sentence does not wait for a cold start.

Installed after `capabilities`, whose tool loop it serves.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import nox
from nox.ai.providers.claude_code import ClaudeCodeProvider
from nox.core.config.hands import McpConfig, McpServerConfig
from nox.core.events import E, Event
from nox.core.logging import get_logger
from nox.core.toolloop import ToolLoop
from nox.mcp.client_ipc import register_mcp_client_ipc
from nox.mcp.gate import McpGate
from nox.mcp.launch import default_launcher
from nox.mcp.servers import ApprovalStore, McpServerManager, configured
from nox.paths import nox_home
from nox.plugins.manager import worker_environment
from nox.settings.layers import resolve_user_config_path, write_user_config

log = get_logger(__name__)

__all__ = ["McpClientRuntime", "McpExtension", "McpRuntime", "install", "prewarm_allowed"]

#: Give the core a moment to finish booting before a CLI process is started next to it.
PREWARM_DELAY_S = 2.0


def prewarm_allowed(provider: ClaudeCodeProvider) -> bool:
    """Whether to start the conversation session ahead of the first turn (tests switch it off)."""
    cfg = provider.config
    return cfg.enabled and cfg.persistent and cfg.prewarm


def _bridge_command(worker_command: list[str]) -> list[str]:
    """The interpreter the workers run with, without their module (`-m nox.worker`)."""
    if len(worker_command) >= 3 and worker_command[-2] == "-m":
        return list(worker_command[:-2])
    return [sys.executable]


class McpRuntime:
    def __init__(
        self,
        gate: McpGate,
        provider: ClaudeCodeProvider,
        *,
        cloud_allowed: Callable[[], bool] = lambda: True,
        tool_loop: Callable[[], ToolLoop | None] = lambda: None,
    ) -> None:
        self.gate = gate
        self.provider = provider
        self.prewarm_task: asyncio.Task[None] | None = None
        self._cloud_allowed = cloud_allowed
        self._tool_loop = tool_loop
        self._unsubs: list[Callable[[], None]] = []

    def watch(self, core: Any) -> None:
        self._unsubs = [
            core.bus.subscribe(E.SECURITY_KILL_SWITCH, self._on_kill),
            core.bus.subscribe(E.SYSTEM_STARTED, self._on_started),
            core.bus.subscribe(E.PRIVACY_MODE_CHANGED, self._on_privacy),
        ]

    async def _on_privacy(self, _event: Event) -> None:
        """Private or offline from now on: a cloud model must stop mid-turn, not at the next one.
        Open turns end (their tokens die, running calls are cancelled) and the answer in progress
        is interrupted; the gate refuses any call that still arrives."""
        if self._cloud_allowed():
            return
        loop = self._tool_loop()
        if loop is not None:
            await loop.end_all()
        self.provider.conversation.halt()
        log.info("mcp.halted_for_privacy")

    async def _on_kill(self, _event: Event) -> None:
        await self.provider.conversation.kill()
        log.warning("mcp.session_killed")

    async def _on_started(self, _event: Event) -> None:
        self.provider.conversation.resume()

    async def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        if self.prewarm_task is not None and not self.prewarm_task.done():
            self.prewarm_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.prewarm_task
        await self.provider.conversation.close()
        await self.gate.close()


class McpClientRuntime:
    """The external servers, tied to the kill switch like every other process Nox starts."""

    def __init__(self, manager: McpServerManager) -> None:
        self.manager = manager
        self.start_task: asyncio.Task[None] | None = None
        self._unsubs: list[Callable[[], None]] = []

    def watch(self, core: Any) -> None:
        self._unsubs = [
            core.bus.subscribe(E.SECURITY_KILL_SWITCH, self._on_kill),
            core.bus.subscribe(E.SYSTEM_STARTED, self._on_started),
        ]

    async def _on_kill(self, _event: Event) -> None:
        await self.manager.halt()
        log.warning("mcp.servers_halted")

    async def _on_started(self, _event: Event) -> None:
        await self.manager.resume()

    async def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        if self.start_task is not None and not self.start_task.done():
            self.start_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.start_task
        await self.manager.stop()


class McpExtension:
    """What `install` returns: the client half always, the bridge half with Claude Code."""

    def __init__(self, client: McpClientRuntime | None, bridge: McpRuntime | None) -> None:
        self.client = client
        self.bridge = bridge

    async def stop(self) -> None:
        if self.client is not None:
            await self.client.stop()
        if self.bridge is not None:
            await self.bridge.stop()


def install_client(core: Any) -> McpClientRuntime | None:
    if core.tool_registry is None:
        return None
    user_path = resolve_user_config_path()

    async def persist(servers: list[McpServerConfig]) -> None:
        data = configured(servers)
        await asyncio.to_thread(write_user_config, user_path, {"mcp": {"servers": data}})
        core.config.mcp = McpConfig.model_validate(
            {**core.config.mcp.model_dump(), "servers": data}
        )

    manager = McpServerManager(
        config=lambda: core.config.mcp,
        registry=core.tool_registry,
        launcher=default_launcher(getattr(core, "job", None)),
        http_client=core.security.egress.client,
        secrets=core.security.secrets,
        approvals=ApprovalStore(nox_home() / "mcp" / "approved.json"),
        workdir=nox_home() / "mcp" / "servers",
        allows_cloud=core.security.privacy.allows_cloud,
        persist=persist,
        audit=core.security.audit,
        # Only when the home plugin is in use: its default host is loopback, which is also where
        # a local MCP server over HTTP would live.
        home_host=lambda: (
            str(core.config.home.host or "") if "home" in core.config.plugins.enabled else ""
        ),
        env_base=worker_environment,
        client_version=nox.__version__,
        revoke_grants=lambda prefix: core.security.engine.revoke_scope(prefix, by="mcp"),
    )
    if core.registry is not None:
        register_mcp_client_ipc(core.registry, manager)
    runtime = McpClientRuntime(manager)
    runtime.watch(core)
    runtime.start_task = asyncio.create_task(manager.start(), name="nox-mcp-servers")
    return runtime


def install(core: Any) -> McpExtension | None:
    client = install_client(core)
    bridge = install_bridge(core)
    if client is None and bridge is None:
        return None
    return McpExtension(client, bridge)


def install_bridge(core: Any) -> McpRuntime | None:
    provider = next((p for p in core.ai_providers or [] if isinstance(p, ClaudeCodeProvider)), None)
    if provider is None or core.hub is None or core.tokens is None:
        log.warning("mcp.not_installed", reason="no Claude Code provider or no hub")
        return None
    orchestrator = core.orchestrator
    privacy = core.security.privacy
    gate = McpGate(
        cloud_allowed=privacy.allows_cloud,
        on_bridge_lost=provider.conversation.bridge_lost,
        hub=core.hub,
        hub_url=lambda: core.hub.url,
        tokens=core.tokens,
        bus=core.bus,
        tool_loop=lambda: orchestrator.tool_loop if orchestrator is not None else None,
        runtime_dir=Path(core.config.paths.runtime_dir),
        command=_bridge_command(list(core.worker_command)),
    )
    gate.register(core.registry)
    provider.conversation.attach_tools(gate)
    runtime = McpRuntime(
        gate,
        provider,
        cloud_allowed=privacy.allows_cloud,
        tool_loop=lambda: orchestrator.tool_loop if orchestrator is not None else None,
    )
    runtime.watch(core)
    if prewarm_allowed(provider):
        runtime.prewarm_task = asyncio.create_task(_prewarm(core, provider), name="nox-prewarm")
    return runtime


async def _prewarm(core: Any, provider: ClaudeCodeProvider) -> None:
    await asyncio.sleep(PREWARM_DELAY_S)
    if not core.security.privacy.allows_cloud():
        return
    try:
        provider.executable()  # not installed: nothing to warm, the router will say why
        loop = core.orchestrator.tool_loop
        offer = loop.current_offer_names() if loop is not None else []
        await provider.conversation.prewarm(
            core.orchestrator.system_prompt(), core.security.privacy.mode.value, offer
        )
    except Exception as exc:  # noqa: BLE001 - a failed prewarm only means a cold first turn
        log.info("mcp.prewarm_failed", error=f"{type(exc).__name__}: {exc}")

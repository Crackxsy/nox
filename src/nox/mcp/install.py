"""`install(core)`: connects the Claude Code conversation session to Nox's tools.

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

from nox.ai.providers.claude_code import ClaudeCodeProvider
from nox.core.events import E, Event
from nox.core.logging import get_logger
from nox.mcp.gate import McpGate

log = get_logger(__name__)

__all__ = ["McpRuntime", "install", "prewarm_allowed"]

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
    def __init__(self, gate: McpGate, provider: ClaudeCodeProvider) -> None:
        self.gate = gate
        self.provider = provider
        self.prewarm_task: asyncio.Task[None] | None = None
        self._unsubs: list[Callable[[], None]] = []

    def watch(self, core: Any) -> None:
        self._unsubs = [
            core.bus.subscribe(E.SECURITY_KILL_SWITCH, self._on_kill),
            core.bus.subscribe(E.SYSTEM_STARTED, self._on_started),
        ]

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


def install(core: Any) -> McpRuntime | None:
    provider = next((p for p in core.ai_providers or [] if isinstance(p, ClaudeCodeProvider)), None)
    if provider is None or core.hub is None or core.tokens is None:
        log.warning("mcp.not_installed", reason="no Claude Code provider or no hub")
        return None
    orchestrator = core.orchestrator
    gate = McpGate(
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
    runtime = McpRuntime(gate, provider)
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
        await provider.conversation.prewarm(
            core.orchestrator.system_prompt(), core.security.privacy.mode.value
        )
    except Exception as exc:  # noqa: BLE001 - a failed prewarm only means a cold first turn
        log.info("mcp.prewarm_failed", error=f"{type(exc).__name__}: {exc}")

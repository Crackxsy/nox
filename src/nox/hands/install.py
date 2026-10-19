"""`install(core)`: register the hands tools (shell, web, packages, mic) onto a booted core.

The sandbox is built once and shared: the shell tool and, later, self-built skills and foreign MCP
servers all run through the same `Sandbox`, so there is one place where confinement and the egress
policy live. If no sandbox can be built on this machine, the factory returns None and the shell
tool refuses every call - running a command unconfined is never a fallback.

What gets registered depends on configuration, the same way the file tools do: `shell.run` and the
package tools only when the user turned them on, `web.*` whenever the web is allowed, `mic` and the
consent store whenever the database is present.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Protocol

from nox.core.config import NoxConfig
from nox.core.extension import ExtensionRuntime
from nox.core.logging import get_logger
from nox.hands.consents import ConsentRepository
from nox.hands.packages import register_package_tools
from nox.hands.shell import ShellSettings, register_shell_tools
from nox.hands.web import WebSettings, guarded_web_client, register_web_tools
from nox.sandbox import Sandbox, SandboxUnavailable, make_sandbox
from nox.sandbox.netguard import NetGuard, resolve_all
from nox.tools.registry import ToolRegistry

log = get_logger(__name__)

__all__ = ["install"]


class CoreLike(Protocol):
    config: NoxConfig
    tool_registry: ToolRegistry
    security: Any
    db: Any


def _shell_program() -> tuple[str, ...]:
    import sys

    if sys.platform == "win32":
        return (
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
        )
    return ("/bin/bash", "-lc")


def _net_guard(config: NoxConfig) -> NetGuard:
    ha_host = config.home.host.strip().lower()
    ha_ips: set[str] = set()
    with contextlib.suppress(OSError, ValueError):
        ha_ips = {str(ip) for ip in resolve_all(ha_host, config.home.port)}
    return NetGuard(home_assistant_ips=frozenset(ha_ips), blocked_hosts=frozenset({ha_host}))


def install(core: CoreLike) -> ExtensionRuntime:
    config = core.config
    registry = core.tool_registry
    guard = _net_guard(config)

    sandbox_cache: list[Sandbox | None] = []

    def sandbox_factory() -> Sandbox | None:
        if not sandbox_cache:
            try:
                sandbox_cache.append(
                    make_sandbox(
                        home_assistant_ips=guard.home_assistant_ips,
                        blocked_hosts=guard.blocked_hosts,
                        prefer_appcontainer=config.hands.windows_appcontainer,
                    )
                )
            except SandboxUnavailable as exc:
                log.warning("hands.sandbox_unavailable", error=str(exc))
                sandbox_cache.append(None)
        return sandbox_cache[0]

    register_shell_tools(
        registry,
        sandbox_factory,
        lambda: ShellSettings(
            enabled=config.hands.shell_enabled,
            shell_program=_shell_program(),
            timeout_s=config.hands.shell_timeout_s,
        ),
    )

    if config.hands.web_enabled:
        # web.fetch may reach any public page, so it does not go through the allow-list egress
        # guard (that is default-deny); it uses a NetGuard-pinned client that blocks only the LAN
        # and pins the checked IP. The privacy engine still gates web.* to FULL/BALANCED.
        register_web_tools(
            registry,
            guard=guard,
            client_factory=lambda: guarded_web_client(guard, timeout=20.0),
            settings=lambda: WebSettings(
                enabled=config.hands.web_enabled,
                searxng_base_url=config.hands.searxng_base_url,
                read_only_hosts=tuple(config.security.read_only_hosts),
            ),
        )

    if config.hands.packages_enabled:
        register_package_tools(registry, _real_package_runner)

    if getattr(core, "db", None) is not None:
        consents = ConsentRepository(core.db)
        _maybe_register_mic(registry, consents)

    log.info(
        "hands.installed",
        shell=config.hands.shell_enabled,
        web=config.hands.web_enabled,
        packages=config.hands.packages_enabled,
        sandbox=sandbox_factory() is not None,
    )
    return None


async def _real_package_runner(argv: list[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _maybe_register_mic(registry: ToolRegistry, consents: ConsentRepository) -> None:
    """Register `mic.record_long` with the consent gate. The recording backend is wired by the
    voice layer; until it is, the transcriber refuses so the tool never records blindly."""
    from nox.hands.mic import register_mic_tool

    async def _unwired(_seconds: int) -> str:
        raise RuntimeError("no transcription backend is wired yet")

    register_mic_tool(registry, consents=consents, transcribe=_unwired)

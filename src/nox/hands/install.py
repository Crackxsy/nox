"""`install(core)`: register the hands tools (shell, web, packages, mic, browser, senses).

The sandbox is built once and shared: the shell tool and, later, self-built skills and foreign MCP
servers all run through the same `Sandbox`, so there is one place where confinement and the egress
policy live. If no sandbox can be built on this machine, the factory returns None and the shell
tool refuses every call - running a command unconfined is never a fallback.

What gets registered depends on configuration, the same way the file tools do: `shell.run` and the
package tools only when the user turned them on, `web.*` whenever the web is allowed, `mic` and the
consent store whenever the database is present, `browser.*` and the senses (`screen.capture`,
`camera.snapshot`, `vision.describe`) unless switched off. The returned runtime closes the browser
on shutdown and lets a test or the benchmark swap the capture backends (`runtime.senses`).
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from nox.core.config import NoxConfig
from nox.core.extension import ExtensionRuntime
from nox.core.logging import get_logger
from nox.hands.browser import BrowserSession, BrowserSettings, register_browser_tools
from nox.hands.consent_tools import register_consent_ipc, register_consent_tools
from nox.hands.consents import ConsentRepository
from nox.hands.packages import register_package_tools
from nox.hands.senses import ClaudeVision, OllamaVision, Senses, register_sense_tools
from nox.hands.shell import ShellSettings, register_shell_tools
from nox.hands.web import (
    WebSettings,
    guarded_web_client,
    register_web_tools,
    search_backend_client,
)
from nox.paths import nox_home
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


@dataclass(slots=True)
class HandsRuntime:
    """What `install` leaves running: the browser (closed on stop) and the senses."""

    browser: BrowserSession | None = None
    senses: Senses | None = None

    async def stop(self) -> None:
        if self.browser is not None:
            await self.browser.close()


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
                        accept_weak=config.hands.accept_weak_sandbox,
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
            trusted_dirs=tuple(config.hands.trusted_program_dirs),
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
            # The one LAN exception: exactly the origin the user configured as search backend.
            search_client_factory=lambda base: search_backend_client(
                base, guard, home_assistant_port=config.home.port
            ),
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
        register_consent_tools(registry, consents)
        ipc = getattr(core, "registry", None)
        if ipc is not None:
            register_consent_ipc(ipc, consents, getattr(core.security, "gate", None))

    runtime = HandsRuntime()
    captures = Path(config.paths.runtime_dir) / "hands" / "captures"
    if config.hands.browser_enabled:
        runtime.browser = BrowserSession(
            lambda: BrowserSettings(
                enabled=config.hands.browser_enabled,
                profile_dir=str(nox_home() / "browser" / "profile"),
                screenshots_dir=str(captures / "browser"),
            ),
            guard,
        )
        register_browser_tools(registry, runtime.browser)
    privacy = getattr(getattr(core, "security", None), "privacy", None)
    if config.hands.senses_enabled and privacy is not None:
        runtime.senses = _build_senses(core, captures, privacy)
        register_sense_tools(registry, runtime.senses)

    log.info(
        "hands.installed",
        shell=config.hands.shell_enabled,
        web=config.hands.web_enabled,
        packages=config.hands.packages_enabled,
        browser=runtime.browser is not None,
        senses=runtime.senses is not None,
        sandbox=sandbox_factory() is not None,
    )
    return runtime


def _build_senses(core: CoreLike, captures: Path, privacy: Any) -> Senses:
    """The senses with their real backends: mss/OpenCV grabbers, Ollama and Claude vision."""
    config = core.config
    ollama_cfg = config.ai.providers.ollama
    egress = getattr(core.security, "egress", None)

    def gpu_allowed() -> bool:
        state = getattr(getattr(core, "state", None), "state", None)
        mode = getattr(getattr(state, "assistant", None), "mode", None)
        return mode is None or str(getattr(mode, "value", mode)) in ollama_cfg.gpu_allowed_modes

    senses = Senses(captures_root=captures, privacy=privacy)
    if egress is not None and ollama_cfg.enabled:
        local = OllamaVision(
            lambda: egress.client(timeout=30.0),
            base_url=ollama_cfg.base_url,
            preferred=config.hands.vision_model,
            gpu_allowed=gpu_allowed,
        )
        senses.local_vision = local.describe
        senses.local_model = local.model

    claude = next(
        (p for p in getattr(core, "ai_providers", None) or [] if p.info.id == "claude_code"),
        None,
    )
    if claude is not None:

        def command() -> list[str]:
            return [*claude.executable(), *claude.build_args("")]

        senses.cloud_vision = ClaudeVision(command, env=getattr(claude, "_env", None)).describe

    bus = getattr(core, "bus", None)
    if bus is not None:

        async def indicator(kind: str, active: bool) -> None:
            from nox.core.events import E, Event

            state = privacy.effective_capture()
            if active:
                state = state.model_copy(update={kind: True})
            await bus.publish(Event(name=E.PRIVACY_CAPTURE_CHANGED, payload=state.model_dump()))

        senses.indicator = indicator
    return senses


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

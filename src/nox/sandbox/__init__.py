"""The sandbox: run a command, foreign code or a skill's tests without it escaping the machine.

`Sandbox` is the one thing the rest of Nox uses. It owns the loopback egress proxy and delegates
confinement to the OS backend (`windows.WindowsSandbox` or `linux.LinuxSandbox`), both of which
present the same `run(argv, cwd, env, egress, limits) -> Result`. A caller declares what network a
run may reach (`EgressDecl`); the sandbox starts a proxy that enforces it plus the non-negotiable
`NetGuard` policy (no loopback, no LAN, no Home Assistant), points the child at it, and tears it
down when the run ends.

`make_sandbox()` picks the backend for this OS and raises `SandboxUnavailable` when none can be set
up - the caller then refuses the tool rather than running anything unconfined (fail closed).
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from typing import Protocol

from nox.core.logging import get_logger
from nox.sandbox.model import (
    EgressDecl,
    Guarantees,
    Limits,
    Result,
    SandboxMode,
    SandboxUnavailable,
)
from nox.sandbox.netguard import NetGuard
from nox.sandbox.proxy import AuditFn, EgressProxy

log = get_logger(__name__)

__all__ = [
    "EgressDecl",
    "EgressProxy",
    "Guarantees",
    "Limits",
    "Result",
    "Sandbox",
    "SandboxMode",
    "SandboxUnavailable",
    "make_sandbox",
]


class Backend(Protocol):
    """What a per-OS backend offers the `Sandbox` wrapper (Windows / Linux implementations)."""

    mode: SandboxMode

    def guarantees(self) -> Guarantees: ...

    async def run(
        self,
        argv: list[str],
        *,
        cwd: str | None,
        egress: EgressDecl,
        limits: Limits,
        proxy_port: int | None,
        proxy_socket: str | None = None,
    ) -> Result: ...


class Sandbox:
    """Confined execution with a vetted network path. Build it with `make_sandbox`.

    A backend whose `Guarantees` are not `strong` (the Windows low-integrity fallback, or an
    AppContainer on a machine where Windows does not enforce loopback isolation) is refused for
    every run unless `accept_weak` was set - which only the user can do, through
    `hands.accept_weak_sandbox`. Skill tests, MCP servers and shell commands all rely on the file
    and network confinement, so "it ran, just less confined" is never decided silently.
    """

    def __init__(
        self,
        backend: Backend,
        *,
        guard: NetGuard,
        audit: AuditFn | None = None,
        accept_weak: bool = False,
    ) -> None:
        self._backend = backend
        self._guard = guard
        self._audit = audit
        self._accept_weak = accept_weak
        self.mode: SandboxMode = backend.mode

    def guarantees(self) -> Guarantees:
        return self._backend.guarantees()

    def check_usable(self) -> Guarantees:
        """The guarantees, or `SandboxUnavailable` when they are weaker than accepted."""
        guarantees = self.guarantees()
        if not guarantees.strong and not self._accept_weak:
            raise SandboxUnavailable(
                f"the {guarantees.backend} sandbox is weaker than required "
                f"({'; '.join(guarantees.notes) or 'not all guarantees hold'}); it runs only if "
                "the user explicitly accepts the weaker mode (hands.accept_weak_sandbox)"
            )
        return guarantees

    async def run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        egress: EgressDecl = EgressDecl.NONE,  # type: ignore[attr-defined]
        limits: Limits | None = None,
    ) -> Result:
        """Run `argv` confined to a work directory, reaching only the declared network (if any)."""
        import asyncio

        # The first call may measure the backend (the Windows loopback probe): off the event loop.
        await asyncio.to_thread(self.check_usable)
        limits = limits or Limits()
        proxy: EgressProxy | None = None
        port: int | None = None
        socket_path: str | None = None
        if egress.wants_network:
            proxy = EgressProxy(allow=egress.hosts, guard=self._guard, audit=self._audit)
            transport = getattr(self._backend, "proxy_transport", "tcp")
            if transport == "pipe":
                socket_path = await proxy.start_pipe(str(self._backend.container_sid))  # type: ignore[attr-defined]
            elif transport == "tcp":
                port = await proxy.start()
            else:
                raise SandboxUnavailable(
                    f"the {self.mode.value} sandbox cannot confine network access; a run that "
                    "needs the network is refused"
                )
        try:
            return await self._backend.run(
                argv,
                cwd=cwd,
                egress=egress,
                limits=limits,
                proxy_port=port,
                proxy_socket=socket_path,
            )
        finally:
            if proxy is not None:
                await proxy.stop()


def make_sandbox(
    *,
    home_assistant_ips: Iterable[str] = (),
    blocked_hosts: Iterable[str] = (),
    audit: AuditFn | None = None,
    prefer_appcontainer: bool = True,
    accept_weak: bool = False,
) -> Sandbox:
    """A `Sandbox` for this OS, or `SandboxUnavailable` when none can be built (fail closed)."""
    guard = NetGuard(
        home_assistant_ips=frozenset(home_assistant_ips),
        blocked_hosts=frozenset(h.strip().lower() for h in blocked_hosts),
    )
    backend: Backend
    if sys.platform == "win32":
        from nox.sandbox.windows import WindowsSandbox

        backend = WindowsSandbox(prefer_appcontainer=prefer_appcontainer)
    elif sys.platform.startswith("linux"):
        from nox.sandbox.linux import LinuxSandbox

        backend = LinuxSandbox()
    else:
        raise SandboxUnavailable(f"no sandbox backend for platform {sys.platform!r}")
    sandbox = Sandbox(backend, guard=guard, audit=audit, accept_weak=accept_weak)
    log.info("sandbox.ready", backend=sandbox.mode.value, accept_weak=accept_weak)
    return sandbox

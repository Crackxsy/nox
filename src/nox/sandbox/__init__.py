"""The sandbox: run a command, foreign code or a skill's tests without it escaping the machine.

`Sandbox` is the one thing the rest of Nox uses. It owns the loopback egress proxy and delegates
confinement to the OS backend (`windows.WindowsSandbox` or `linux.LinuxSandbox`), both of which
present the same `run(argv, cwd, env, egress, limits) -> Result`. A caller declares what network a
run may reach (`EgressDecl`); the sandbox starts a proxy that enforces it plus the non-negotiable
`NetGuard` policy (no loopback, no LAN, no Home Assistant), points the child at it, and tears it
down when the run ends.

`make_sandbox()` picks the backend for this OS and raises `SandboxUnavailable` when none can be set
up - the caller then refuses the tool rather than running anything unconfined (fail closed).

Two ways to run something, with the same confinement:

* `await sandbox.run(argv, cwd=None, env=None, egress=EgressDecl.NONE, limits=None) -> Result` -
  a command that finishes: output captured and truncated, killed at `limits.timeout_s`.
* `await sandbox.start(argv, cwd=None, env=None, egress=EgressDecl.NONE, limits=None,
  lifetime_s=None, on_exit=None) -> SandboxProcess` - a long-running server (an MCP stdio server,
  a watcher). It talks over its pipes (`send(bytes)`, `readline()`), keeps a bounded
  `stderr_tail()`, and ends with `stop(grace_s)` (stdin closed, then the tree is killed) or
  `kill()`; `async with` stops it. `limits.timeout_s` does not apply - `lifetime_s` does, when
  given. The egress proxy lives exactly as long as the process. The tree dies with the core
  (job object kill-on-close / `bwrap --die-with-parent`). `on_exit(code)` plus `RestartPolicy`
  are the hooks for a supervisor; the sandbox never restarts anything by itself.

**`EgressDecl(hosts=("*",))` means "any public host, any port, through the proxy".** It is what
a network-enabled MCP server declares. It is not a hole: every connection still goes through the
run's own proxy, which resolves the name and lets `NetGuard` refuse loopback, the LAN, link-local
and cloud-metadata addresses and the Home Assistant host - so `*` can never reach anything inside
the home. Whether a server may have `*` at all is decided before `start` by the caller's
permission check (installing a network-enabled server is confirmed), not by the sandbox.

Declared egress goes through a loopback proxy that exists for that one run or process and
demands its own random credentials, which only the child receives (in its proxy URL): another
local program - another run's child included - cannot use it.

`env` adds variables to the child's minimal environment (an MCP server's settings); the keys that
carry the confinement (`PATH`, `HOME`/`USERPROFILE`/`TEMP`, the proxy variables, `LD_PRELOAD`,
...) are refused with `ValueError` (`model.RESERVED_ENV_KEYS`). Nothing from the core's own
environment is inherited beyond the backend's fixed safe list.

`guarantees()` reports what this machine's backend really enforces (weaker on the Windows
low-integrity fallback), for `run` and `start` alike.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Protocol

from nox.core.logging import get_logger
from nox.sandbox.model import (
    EgressDecl,
    Guarantees,
    Limits,
    Result,
    SandboxMode,
    SandboxUnavailable,
    checked_env,
)
from nox.sandbox.netguard import NetGuard
from nox.sandbox.process import MessageTooLarge, ProcessIo, RestartPolicy, SandboxProcess
from nox.sandbox.proxy import AuditFn, EgressProxy

log = get_logger(__name__)

__all__ = [
    "EgressDecl",
    "EgressProxy",
    "Guarantees",
    "Limits",
    "MessageTooLarge",
    "RestartPolicy",
    "Result",
    "Sandbox",
    "SandboxMode",
    "SandboxProcess",
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
        extra_env: Mapping[str, str] | None = None,
        proxy_auth: str = "",
        proxy_socket: str | None = None,
    ) -> Result: ...

    async def start(
        self,
        argv: list[str],
        *,
        cwd: str | None,
        egress: EgressDecl,
        limits: Limits,
        proxy_port: int | None,
        extra_env: Mapping[str, str] | None = None,
        proxy_auth: str = "",
        proxy_socket: str | None = None,
    ) -> ProcessIo: ...


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

    async def _check_usable_async(self) -> None:
        import asyncio

        # The first call may measure the backend (the Windows loopback probe): off the event loop.
        await asyncio.to_thread(self.check_usable)

    async def _open_proxy(self, egress: EgressDecl) -> EgressProxy | None:
        """Start the run's own proxy when it declared egress, on the transport its backend needs:
        loopback TCP, or a named pipe only the backend's AppContainer may open. A backend that
        offers no transport its child is held to (`"none"`) gets no network at all."""
        if not egress.wants_network:
            return None
        proxy = EgressProxy(
            allow=egress.hosts,
            guard=self._guard,
            audit=self._audit,
            credentials=EgressProxy.new_credentials(),
        )
        transport = getattr(self._backend, "proxy_transport", "tcp")
        if transport == "pipe":
            await proxy.start_pipe(str(self._backend.container_sid))  # type: ignore[attr-defined]
        elif transport == "tcp":
            await proxy.start()
        else:
            raise SandboxUnavailable(
                f"the {self.mode.value} sandbox cannot confine network access; a run that "
                "needs the network is refused"
            )
        return proxy

    async def run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        egress: EgressDecl = EgressDecl.NONE,  # type: ignore[attr-defined]
        limits: Limits | None = None,
    ) -> Result:
        """Run `argv` confined to a work directory, reaching only the declared network (if any)."""
        extra = checked_env(dict(env) if env else None)
        await self._check_usable_async()
        limits = limits or Limits()
        proxy = await self._open_proxy(egress)
        try:
            return await self._backend.run(
                argv,
                cwd=cwd,
                egress=egress,
                limits=limits,
                extra_env=extra,
                **_proxy_kwargs(proxy),
            )
        finally:
            if proxy is not None:
                await proxy.stop()

    async def start(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        egress: EgressDecl = EgressDecl.NONE,  # type: ignore[attr-defined]
        limits: Limits | None = None,
        lifetime_s: float | None = None,
        on_exit: Callable[[int | None], None] | None = None,
    ) -> SandboxProcess:
        """Start a long-running confined process with stdio pipes; see the module docstring.

        Same confinement and egress rules as `run`. Raises `SandboxUnavailable` (nothing started)
        when the backend cannot confine it, and `ValueError` for a reserved `env` key.
        """
        extra = checked_env(dict(env) if env else None)
        if lifetime_s is not None and lifetime_s <= 0:
            raise ValueError("lifetime_s must be positive")
        await self._check_usable_async()
        limits = limits or Limits()
        proxy = await self._open_proxy(egress)
        try:
            io = await self._backend.start(
                argv,
                cwd=cwd,
                egress=egress,
                limits=limits,
                extra_env=extra,
                **_proxy_kwargs(proxy),
            )
        except BaseException:
            if proxy is not None:
                await proxy.stop()
            raise
        if proxy is not None:
            backend_cleanup = io.cleanup
            running_proxy = proxy

            async def cleanup() -> None:
                try:
                    await backend_cleanup()
                finally:
                    await running_proxy.stop()

            io.cleanup = cleanup
        log.info("sandbox.process_started", pid=io.pid, backend=self.mode.value)
        return SandboxProcess(
            io,
            max_message_bytes=limits.max_message_bytes,
            stderr_bytes=limits.max_output_bytes,
            lifetime_s=lifetime_s,
            on_exit=on_exit,
        )


def _proxy_kwargs(proxy: EgressProxy | None) -> dict[str, Any]:
    """Where the backend's child finds its proxy (a loopback port or a socket/pipe path) and the
    per-run credentials it must present."""
    if proxy is None:
        return {"proxy_port": None, "proxy_auth": ""}
    kwargs: dict[str, Any] = {
        "proxy_port": proxy.port or None,
        "proxy_auth": ":".join(proxy.credentials or ()),
    }
    if proxy.socket_path:
        kwargs["proxy_socket"] = proxy.socket_path
    return kwargs


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

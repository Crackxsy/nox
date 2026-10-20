"""A named-pipe listener for the egress proxy, readable by exactly one AppContainer.

Why a pipe: an AppContainer child has no network capability, and whether it may reach a TCP port
on the host's loopback depends on Windows network isolation (blocked where loopback isolation is
enforced, which is the documented default). A named pipe whose DACL names the container SID is
reachable either way, so the proxy also listens here, and a tiny forwarder *inside* the container
(`windows.FORWARDER_PS`) offers the child an ordinary `127.0.0.1:<port>` - loopback to its own
container, which network isolation allows - and relays every connection into this pipe.

Every connection that arrives is handed to the same `EgressProxy` handler as a TCP one, so the
policy (declared hosts, NetGuard) is applied in exactly one place.

This uses the private asyncio proactor API (`accept_pipe`, `_make_duplex_pipe_transport`) the way
`ProactorEventLoop.start_serving_pipe` does, because that public method offers no way to set a
security descriptor - and the default pipe DACL would let neither the container in nor keep other
users out. On any other event loop the pipe cannot be served and the run fails closed.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from nox.core.logging import get_logger
from nox.sandbox.model import SandboxUnavailable

log = get_logger(__name__)

__all__ = ["PipeListener", "pipe_sddl"]

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]

_PIPE_ACCESS_DUPLEX = 0x00000003
_FILE_FLAG_OVERLAPPED = 0x40000000
_FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
_PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
_PIPE_UNLIMITED_INSTANCES = 255
_BUFSIZE = 64 * 1024


def pipe_sddl(container_sid: str) -> str:
    """Owner and SYSTEM full access, the one container SID read/write, nobody else; labelled low so
    a low-integrity (AppContainer) client may write to it. Protected: nothing is inherited."""
    return f"D:P(A;;GA;;;OW)(A;;GA;;;SY)(A;;GRGW;;;{container_sid})S:(ML;;NW;;;LW)"


def new_pipe_name() -> str:
    """An unguessable pipe path (128 random bits): one per run, never reused."""
    return "\\\\.\\pipe\\nox-egress-" + secrets.token_hex(16)


class PipeListener:
    """Serves `handler` on a fresh named pipe until `close()`."""

    def __init__(self, *, sddl: str, handler: Handler) -> None:
        self.path = new_pipe_name()
        self._sddl = sddl
        self._handler = handler
        self._task: asyncio.Task[None] | None = None
        self._pending: Any = None
        self._connections: set[asyncio.Task[None]] = set()
        self._sd = ctypes.c_void_p()
        self._sa: Any = None

    async def start(self) -> str:
        from nox.sandbox import _win32

        loop: Any = asyncio.get_running_loop()
        if not hasattr(loop, "_proactor"):
            raise SandboxUnavailable("the egress pipe needs the Windows proactor event loop")
        if not _win32._adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            self._sddl, _win32.SDDL_REVISION_1, ctypes.byref(self._sd), None
        ):
            raise _win32._fail("ConvertStringSecurityDescriptorToSecurityDescriptorW(egress pipe)")
        self._sa = _win32._SecurityAttributes(
            ctypes.sizeof(_win32._SecurityAttributes), self._sd, False
        )
        first = self._instance(first=True)  # fails if anyone already holds the name
        self._task = asyncio.create_task(self._accept_loop(loop, first), name="nox-egress-pipe")
        log.info("sandbox.pipe_listening")
        return self.path

    def _instance(self, *, first: bool) -> Any:
        import _winapi
        from asyncio import windows_utils

        flags = _PIPE_ACCESS_DUPLEX | _FILE_FLAG_OVERLAPPED
        if first:
            flags |= _FILE_FLAG_FIRST_PIPE_INSTANCE
        handle = _winapi.CreateNamedPipe(  # type: ignore[attr-defined,unused-ignore]
            self.path,
            flags,
            _PIPE_REJECT_REMOTE_CLIENTS,
            _PIPE_UNLIMITED_INSTANCES,
            _BUFSIZE,
            _BUFSIZE,
            0,
            ctypes.addressof(self._sa),
        )
        return windows_utils.PipeHandle(handle)  # type: ignore[attr-defined,unused-ignore]

    async def _accept_loop(self, loop: Any, pipe: Any) -> None:
        try:
            while True:
                self._pending = pipe
                await loop._proactor.accept_pipe(pipe)
                self._pending = None
                connected, pipe = pipe, self._instance(first=False)
                task = asyncio.create_task(self._serve(loop, connected))
                self._connections.add(task)
                task.add_done_callback(self._connections.discard)
        except asyncio.CancelledError:
            pass
        except OSError as exc:
            log.warning("sandbox.pipe_accept_failed", error=str(exc))
        finally:
            if self._pending is not None:
                with contextlib.suppress(Exception):
                    self._pending.close()
                self._pending = None

    async def _serve(self, loop: Any, pipe: Any) -> None:
        reader = asyncio.StreamReader(limit=_BUFSIZE)
        protocol = asyncio.StreamReaderProtocol(reader)
        transport = loop._make_duplex_pipe_transport(pipe, protocol)
        writer = asyncio.StreamWriter(transport, protocol, reader, loop)
        try:
            await self._handler(reader, writer)
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        for task in list(self._connections):
            task.cancel()
        for task in list(self._connections):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._sd:
            from nox.sandbox import _win32

            _win32._k32.LocalFree(self._sd)
            self._sd = ctypes.c_void_p()

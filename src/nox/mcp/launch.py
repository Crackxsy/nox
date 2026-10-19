"""Starting a local MCP server process, and the seam where the sandbox plugs in.

A local MCP server is somebody else's program. Today it is started the way plugin workers are:
a fixed argv with no shell, an environment reduced to the allow-list plugin workers get
(`nox.plugins.manager.worker_environment`) plus the server's own validated variables, a working
folder of its own under `<nox home>/mcp/<id>`, no console window, inside the core's job object so
it dies with the core, and stderr kept only as a short tail for the status line.

That is process hygiene, not a sandbox: the process still runs as the user. The sandbox (Low
Integrity token and network through the Nox proxy on Windows, `bwrap` on Linux) is built in
`nox.sandbox`. `ServerLauncher` is the interface it implements; `default_launcher()` returns it
when it is installed and says honestly - through `sandboxed` - when it is not.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import subprocess
import sys
from collections import deque
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from nox.core.logging import get_logger
from nox.mcp.client import MAX_MESSAGE_BYTES

log = get_logger(__name__)

__all__ = [
    "LaunchedServer",
    "ProcessLauncher",
    "SandboxLauncher",
    "ServerLauncher",
    "default_launcher",
]

#: Characters of stderr kept for the status line; a server's log is not Nox's to store.
STDERR_TAIL_CHARS = 600


class JobLike(Protocol):
    def assign(self, pid: int) -> bool: ...


class LaunchedServer(Protocol):
    """A started server process: its pipes, its pid, and how to end it."""

    @property
    def pid(self) -> int: ...
    @property
    def reader(self) -> asyncio.StreamReader: ...
    @property
    def writer(self) -> asyncio.StreamWriter: ...
    @property
    def returncode(self) -> int | None: ...
    def stderr_tail(self) -> str: ...
    async def wait(self) -> int: ...
    async def terminate(self, timeout_s: float = 2.0) -> None: ...


class ServerLauncher(Protocol):
    """Starts a server process. `sandboxed` is shown on the dashboard next to the server."""

    @property
    def sandboxed(self) -> bool: ...

    async def launch(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        network: bool,
    ) -> LaunchedServer: ...


class _Process:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        assert process.stdout is not None and process.stdin is not None
        self._tail: deque[str] = deque(maxlen=40)
        self._stderr_task = (
            asyncio.create_task(self._drain(process.stderr), name="nox-mcp-stderr")
            if process.stderr is not None
            else None
        )

    async def _drain(self, stream: asyncio.StreamReader) -> None:
        with contextlib.suppress(Exception):
            while line := await stream.readline():
                self._tail.append(line.decode("utf-8", errors="replace").rstrip())

    @property
    def pid(self) -> int:
        return int(self._process.pid)

    @property
    def reader(self) -> asyncio.StreamReader:
        assert self._process.stdout is not None
        return self._process.stdout

    @property
    def writer(self) -> asyncio.StreamWriter:
        assert self._process.stdin is not None
        return self._process.stdin

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    def stderr_tail(self) -> str:
        return " | ".join(self._tail)[-STDERR_TAIL_CHARS:]

    async def wait(self) -> int:
        return int(await self._process.wait())

    async def terminate(self, timeout_s: float = 2.0) -> None:
        if self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout_s)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self._process.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._process.wait(), timeout_s)
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stderr_task


class ProcessLauncher:
    """Plain subprocess with plugin-worker hygiene; see the module docstring for what it is not."""

    sandboxed = False

    def __init__(self, job: JobLike | None = None) -> None:
        self._job = job

    async def launch(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        network: bool,
    ) -> LaunchedServer:
        del network  # without the sandbox there is no way to take the network away
        await asyncio.to_thread(cwd.mkdir, parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=dict(env),
            limit=MAX_MESSAGE_BYTES,
            **kwargs,
        )
        if self._job is not None:
            self._job.assign(int(process.pid))
        log.info("mcp.server_process_started", pid=process.pid, sandboxed=False)
        return _Process(process)


class _SandboxedServer:
    """A `SandboxProcess` (the sandbox's long-running variant) seen as a `LaunchedServer`."""

    def __init__(self, process: Any) -> None:
        self._process = process

    @property
    def pid(self) -> int:
        return int(getattr(self._process, "pid", 0) or 0)

    @property
    def reader(self) -> asyncio.StreamReader:
        reader: asyncio.StreamReader = self._process.stdout
        return reader

    @property
    def writer(self) -> asyncio.StreamWriter:
        writer: asyncio.StreamWriter = self._process.stdin
        return writer

    @property
    def returncode(self) -> int | None:
        code = getattr(self._process, "returncode", None)
        return None if code is None else int(code)

    def stderr_tail(self) -> str:
        tail = getattr(self._process, "stderr_tail", None)
        return str(tail() if callable(tail) else "")[-STDERR_TAIL_CHARS:]

    async def wait(self) -> int:
        return int(await self._process.wait())

    async def terminate(self, timeout_s: float = 2.0) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._process.terminate(), timeout_s)


class SandboxLauncher:
    """Local MCP servers inside `nox.sandbox`, through its long-running variant
    `Sandbox.start(argv, *, cwd, env, egress, limits)`, which returns a process with asyncio
    `stdin`/`stdout`, `pid`, `returncode`, `wait()` and `terminate()`. A server that declares no
    network gets no egress at all; one that does goes through the sandbox's egress proxy."""

    sandboxed = True

    def __init__(self, sandbox: Any, module: Any) -> None:
        self._sandbox = sandbox
        self._module = module

    async def launch(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        network: bool,
    ) -> LaunchedServer:
        await asyncio.to_thread(cwd.mkdir, parents=True, exist_ok=True)
        egress = self._module.EgressDecl(hosts=("*",)) if network else self._module.EgressDecl()
        process = await self._sandbox.start(
            list(argv), cwd=str(cwd), env=dict(env), egress=egress, limits=self._module.Limits()
        )
        log.info("mcp.server_process_started", sandboxed=True)
        return _SandboxedServer(process)


def default_launcher(job: JobLike | None = None) -> ServerLauncher:
    """The sandbox when it can start long-running processes here, else `ProcessLauncher`.

    `nox.sandbox` is looked up, not imported, so this file works on a checkout without it. Its
    one-shot `run()` is not enough for a server that stays up: only a sandbox with `start()` is
    used, and the dashboard says `sandboxed: false` otherwise.
    """
    try:
        module = importlib.import_module("nox.sandbox")
        sandbox = module.make_sandbox()
    except ImportError:
        return ProcessLauncher(job)
    except Exception as exc:  # noqa: BLE001 - no backend here; the process launcher remains
        log.warning("mcp.sandbox_unavailable", error=f"{type(exc).__name__}: {exc}")
        return ProcessLauncher(job)
    if not callable(getattr(sandbox, "start", None)):
        return ProcessLauncher(job)
    return SandboxLauncher(sandbox, module)

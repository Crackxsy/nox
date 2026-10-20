"""Starting a local MCP server process, and the seam where the sandbox plugs in.

A local MCP server is somebody else's program. Today it is started the way plugin workers are:
a fixed argv with no shell, an environment reduced to the allow-list plugin workers get
(`nox.plugins.manager.worker_environment`) plus the server's own validated variables, a working
folder of its own under `<nox home>/mcp/<id>`, no console window, inside the core's job object so
it dies with the core, and stderr kept only as a short tail for the status line.

That is process hygiene, not a sandbox: the process still runs as the user. The sandbox (Low
Integrity token and network through the Nox proxy on Windows, `bwrap` on Linux) is built in
`nox.sandbox`. `ServerLauncher` is the interface it implements; `default_launcher()` returns it
when it can be built. When it cannot (no backend, `bwrap` missing or not the pinned build, the
user-namespace probe failing), local servers fail closed: `RefusingLauncher` starts nothing and
its `refusal` is the honest status line. Only the user's own `mcp.allow_unsandboxed: true` in
`user.yaml` - not settable from the dashboard or by the model - falls back to `ProcessLauncher`.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import subprocess
import sys
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from nox.core.logging import get_logger
from nox.mcp.client import MAX_MESSAGE_BYTES, McpClientError, StdioTransport, Transport

log = get_logger(__name__)

__all__ = [
    "LaunchedServer",
    "ProcessLauncher",
    "RefusingLauncher",
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
    def returncode(self) -> int | None: ...
    def transport(self) -> Transport:
        """The JSON-RPC line transport over the process's stdin/stdout."""

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

    def transport(self) -> Transport:
        return StdioTransport(self.reader, self.writer)

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


class SandboxLineTransport:
    """Newline-delimited JSON-RPC over a `SandboxProcess` (`send(bytes)` / `readline()`)."""

    def __init__(self, process: Any) -> None:
        self._process = process

    async def send(self, message: dict[str, Any]) -> None:
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + chr(10)
        try:
            await self._process.send(data.encode("utf-8"))
        except (BrokenPipeError, OSError) as exc:
            raise McpClientError("the server has exited") from exc

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            try:
                line = await self._process.readline()
            except ValueError as exc:  # MessageTooLarge
                raise McpClientError("the server sent a message larger than allowed") from exc
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                log.info("mcp.client.non_json_line", size=len(text))
                continue
            if isinstance(message, dict):
                yield message

    async def close(self) -> None:
        return None


class _SandboxedServer:
    """A `SandboxProcess` (`Sandbox.start`) seen as a `LaunchedServer`: its pipes are `send` /
    `readline`, it ends with `stop(grace_s)`, and its stderr is the sandbox's ring buffer."""

    def __init__(self, process: Any) -> None:
        self._process = process

    @property
    def pid(self) -> int:
        return int(self._process.pid)

    @property
    def returncode(self) -> int | None:
        code = self._process.returncode
        return None if code is None else int(code)

    def transport(self) -> Transport:
        return SandboxLineTransport(self._process)

    def stderr_tail(self) -> str:
        tail = str(self._process.stderr_tail()).strip().splitlines()[-40:]
        return " | ".join(tail)[-STDERR_TAIL_CHARS:]

    async def wait(self) -> int:
        code = await self._process.wait()
        return -9 if code is None else int(code)

    async def terminate(self, timeout_s: float = 2.0) -> None:
        with contextlib.suppress(Exception):
            await self._process.stop(grace_s=timeout_s)


class SandboxLauncher:
    """Local MCP servers inside `nox.sandbox`, through `Sandbox.start(argv, *, env, egress,
    limits)` -> `SandboxProcess`. The sandbox owns the server's work folder (a private one per
    start; nothing under `<nox home>` is shared with it). A server that declares no network gets
    no egress at all; one that does goes through its own egress proxy (`*`: public hosts only)."""

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
        # The sandbox's private work folder is used, not `cwd` (a folder under <nox home>); its
        # name is the server's id, and each server runs in a sandbox container of its own.
        egress = self._module.EgressDecl(hosts=("*",)) if network else self._module.EgressDecl()
        process = await self._sandbox.start(
            list(argv),
            env=dict(env),
            egress=egress,
            limits=self._module.Limits(max_message_bytes=MAX_MESSAGE_BYTES),
            workload=f"mcp:{cwd.name}",
        )
        log.info("mcp.server_process_started", pid=process.pid, sandboxed=True)
        return _SandboxedServer(process)


class RefusingLauncher:
    """No usable sandbox and no opt-in: a local server is never started. `refusal` says why; the
    manager shows it as the server's status instead of trying (fail closed)."""

    sandboxed = False

    def __init__(self, refusal: str) -> None:
        self.refusal = refusal

    async def launch(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        network: bool,
    ) -> LaunchedServer:
        del argv, cwd, env, network
        raise McpClientError(self.refusal)


def _without_sandbox(job: JobLike | None, why: str, *, allow_unsandboxed: bool) -> ServerLauncher:
    if allow_unsandboxed:
        log.warning("mcp.sandbox_unavailable_running_unsandboxed", error=why)
        return ProcessLauncher(job)
    log.warning("mcp.sandbox_unavailable_servers_refused", error=why)
    return RefusingLauncher(
        f"no sandbox is available here ({why}), so local MCP servers are not started; "
        "only `mcp.allow_unsandboxed: true` in user.yaml runs them without one"
    )


def default_launcher(
    job: JobLike | None = None,
    *,
    allow_unsandboxed: bool = False,
    home_assistant_ips: Iterable[str] = (),
    blocked_hosts: Iterable[str] = (),
    audit: Callable[[Any], None] | None = None,
    prefer_appcontainer: bool = True,
    accept_weak: bool = False,
) -> ServerLauncher:
    """The sandbox when it can start long-running processes here; otherwise nothing runs.

    `nox.sandbox` is looked up, not imported, so this file works on a checkout without it. Its
    one-shot `run()` is not enough for a server that stays up: only a sandbox with `start()` is
    used. Any failure to build it - a missing module included - fails closed (`RefusingLauncher`)
    unless the user opted into `allow_unsandboxed`. The sandbox gets the same network guard as a
    shell command (`nox.hands.install`): the Home Assistant addresses and host refused, and every
    egress decision audited.
    """
    try:
        module = importlib.import_module("nox.sandbox")
        sandbox = module.make_sandbox(
            home_assistant_ips=tuple(home_assistant_ips),
            blocked_hosts=tuple(blocked_hosts),
            audit=audit,
            prefer_appcontainer=prefer_appcontainer,
            accept_weak=accept_weak,
        )
    except Exception as exc:  # noqa: BLE001 - whatever failed, no server runs unsandboxed
        why = f"{type(exc).__name__}: {exc}"
        return _without_sandbox(job, why, allow_unsandboxed=allow_unsandboxed)
    if not callable(getattr(sandbox, "start", None)):
        why = "this sandbox cannot run long-lived processes"
        return _without_sandbox(job, why, allow_unsandboxed=allow_unsandboxed)
    return SandboxLauncher(sandbox, module)

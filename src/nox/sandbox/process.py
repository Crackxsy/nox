"""A long-running sandboxed process with stdio pipes: what `Sandbox.start` hands back.

`Sandbox.run` is for a command that finishes; a foreign MCP stdio server, a language server or a
watcher has to keep running and talk over its pipes. `SandboxProcess` is that handle. It carries
the same confinement as `run` (the backend starts it exactly the same way) and adds the lifecycle:

* `send(data)` / `readline()` - stdin and stdout. stdout is read by a pump into a bounded queue,
  so a child that writes faster than the caller reads blocks on its own pipe (backpressure)
  instead of growing Nox's memory; one line longer than `Limits.max_message_bytes` raises
  `MessageTooLarge` rather than being buffered without end.
* stderr is drained continuously into a ring buffer of `Limits.max_output_bytes` (`stderr_tail()`)
  so a chatty server can never stall on a full stderr pipe.
* `stop(grace_s)` closes stdin, waits up to `grace_s` for a clean exit, then kills the whole tree.
  `kill()` kills it now. Both are idempotent. `async with` stops on exit.
* Kill-on-close: the tree also dies with the core - the Windows job object is
  `KILL_ON_JOB_CLOSE`, and on Linux `bwrap --die-with-parent` - so a crash of Nox leaves nothing
  running.
* `lifetime_s` (optional) kills the tree after that many seconds, whatever it is doing.
* `on_exit(code)` is called once when the process has ended, for whatever reason. With
  `RestartPolicy` it is the hook a supervisor uses: `policy.delay_for(attempt)` says how long to
  wait before starting it again, or `None` to give up. The sandbox never restarts anything by
  itself - a restarted MCP server needs a new handshake, which only its client can do.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from nox.core.logging import get_logger

log = get_logger(__name__)

__all__ = ["MessageTooLarge", "ProcessIo", "RestartPolicy", "SandboxProcess"]

_CHUNK = 64 * 1024
#: Chunks the stdout queue holds before the pump stops reading (and the child blocks on write).
_QUEUE_CHUNKS = 64


class MessageTooLarge(ValueError):  # noqa: N818 - parallels SandboxUnavailable
    """One stdout line was longer than `Limits.max_message_bytes`; the rest of it is dropped."""


@dataclass(frozen=True, slots=True)
class RestartPolicy:
    """How a supervisor restarts a server that died. Pure; the sandbox itself never restarts.

    `delay_for(attempt)` (attempt 1 is the first restart) returns the back-off in seconds, or
    None once `max_restarts` is used up. A supervisor resets its attempt counter after the process
    has stayed up for `healthy_after_s`.
    """

    max_restarts: int = 3
    backoff_s: tuple[float, ...] = (1.0, 5.0, 30.0)
    healthy_after_s: float = 60.0

    def delay_for(self, attempt: int) -> float | None:
        if attempt < 1 or attempt > self.max_restarts or not self.backoff_s:
            return None
        return self.backoff_s[min(attempt, len(self.backoff_s)) - 1]


@dataclass(slots=True)
class ProcessIo:
    """What a backend gives `SandboxProcess`: raw pipe access and process control.

    Each callable is blocking-free from the loop's point of view (backends wrap blocking calls
    in threads). `read_out`/`read_err` return b"" at EOF.
    """

    pid: int
    read_out: Callable[[int], Awaitable[bytes]]
    read_err: Callable[[int], Awaitable[bytes]]
    write_in: Callable[[bytes], Awaitable[None]]
    close_in: Callable[[], None]
    wait: Callable[[], Awaitable[int | None]]
    kill: Callable[[], None]
    #: Releases handles, removes a temporary work directory, stops the egress proxy.
    cleanup: Callable[[], Awaitable[None]]


class SandboxProcess:
    """Handle on one running sandboxed process. Build it with `Sandbox.start`."""

    def __init__(
        self,
        io: ProcessIo,
        *,
        max_message_bytes: int,
        stderr_bytes: int,
        lifetime_s: float | None = None,
        on_exit: Callable[[int | None], None] | None = None,
    ) -> None:
        self.pid = io.pid
        self._io = io
        self._max_message = max_message_bytes
        self._stderr: deque[bytes] = deque()
        self._stderr_size = 0
        self._stderr_cap = stderr_bytes
        self._out: asyncio.Queue[bytes] = asyncio.Queue(maxsize=_QUEUE_CHUNKS)
        self._buffer = bytearray()
        self._eof = False
        self._out_closed = False
        self._write_lock = asyncio.Lock()
        self._returncode: int | None = None
        self._exited = asyncio.Event()
        self._on_exit = on_exit
        self._killed = False
        self._tasks = [
            asyncio.ensure_future(self._pump_out()),
            asyncio.ensure_future(self._pump_err()),
        ]
        self._waiter = asyncio.ensure_future(self._wait_and_clean())
        self._lifetime: asyncio.Task[None] | None = None
        if lifetime_s is not None:
            self._lifetime = asyncio.ensure_future(self._expire(lifetime_s))

    # -- state ----------------------------------------------------------------------------------

    @property
    def returncode(self) -> int | None:
        """The exit code once the process has ended (None while running, or when killed)."""
        return self._returncode

    @property
    def running(self) -> bool:
        return not self._exited.is_set()

    def stderr_tail(self) -> str:
        """The last `Limits.max_output_bytes` the process wrote to stderr."""
        return b"".join(self._stderr).decode("utf-8", errors="replace")

    # -- stdio ------------------------------------------------------------------------------------

    async def send(self, data: bytes) -> None:
        """Write to the process's stdin. Raises `BrokenPipeError` once it has exited."""
        if not self.running:
            raise BrokenPipeError("the sandboxed process has exited")
        async with self._write_lock:
            await self._io.write_in(data)

    async def readline(self) -> bytes:
        """The next stdout line including its newline; b"" at end of output.

        A line longer than `max_message_bytes` raises `MessageTooLarge`; the over-long line is
        discarded up to its newline, so the next call reads the line after it.
        """
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self._buffer[: newline + 1])
                del self._buffer[: newline + 1]
                if len(line) > self._max_message:
                    raise MessageTooLarge(f"stdout line of {len(line)} bytes")
                return line
            if len(self._buffer) > self._max_message:
                await self._discard_rest_of_line()
                raise MessageTooLarge(f"stdout line over {self._max_message} bytes")
            if self._eof or (self._out_closed and self._out.empty()):
                self._eof = True
                line = bytes(self._buffer)
                self._buffer.clear()
                return line
            chunk = await self._out.get()
            if not chunk:
                self._eof = True
            else:
                self._buffer += chunk

    async def _discard_rest_of_line(self) -> None:
        self._buffer.clear()
        while not self._eof:
            if self._out_closed and self._out.empty():
                self._eof = True
                return
            chunk = await self._out.get()
            if not chunk:
                self._eof = True
                return
            newline = chunk.find(b"\n")
            if newline >= 0:
                self._buffer += chunk[newline + 1 :]
                return

    # -- lifecycle --------------------------------------------------------------------------------

    async def wait(self, timeout: float | None = None) -> int | None:
        """Wait for the process to end; returns its exit code. Raises `TimeoutError`."""
        await asyncio.wait_for(self._exited.wait(), timeout)
        return self._returncode

    def kill(self) -> None:
        """Kill the whole process tree now. Idempotent; never waits."""
        if self._exited.is_set():
            return
        self._killed = True
        with contextlib.suppress(OSError):
            self._io.kill()

    async def stop(self, grace_s: float = 2.0) -> int | None:
        """Close stdin, give the process `grace_s` to exit on its own, then kill the tree."""
        if self.running:
            with contextlib.suppress(OSError):
                self._io.close_in()
            try:
                await asyncio.wait_for(asyncio.shield(self._exited.wait()), grace_s)
            except TimeoutError:
                self.kill()
        await self._exited.wait()
        await asyncio.shield(self._waiter)
        return self._returncode

    async def __aenter__(self) -> SandboxProcess:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    # -- internals --------------------------------------------------------------------------------

    async def _pump_out(self) -> None:
        try:
            while True:
                chunk = await self._io.read_out(_CHUNK)
                if not chunk:
                    break
                await self._out.put(chunk)
        except (OSError, ValueError) as exc:
            log.info("sandbox.process_stdout_closed", pid=self.pid, error=type(exc).__name__)
        finally:
            self._out_closed = True
            with contextlib.suppress(asyncio.QueueFull):
                self._out.put_nowait(b"")

    async def _pump_err(self) -> None:
        with contextlib.suppress(OSError, ValueError):
            while True:
                chunk = await self._io.read_err(_CHUNK)
                if not chunk:
                    return
                self._stderr.append(chunk)
                self._stderr_size += len(chunk)
                while self._stderr_size > self._stderr_cap and self._stderr:
                    dropped = self._stderr.popleft()
                    self._stderr_size -= len(dropped)

    async def _expire(self, lifetime_s: float) -> None:
        await asyncio.sleep(lifetime_s)
        if self.running:
            log.warning("sandbox.process_lifetime_exceeded", pid=self.pid, lifetime_s=lifetime_s)
            self.kill()

    async def _wait_and_clean(self) -> None:
        code: int | None = None
        try:
            code = await self._io.wait()
        finally:
            # The tree may still hold children after the main process ended; end them too.
            with contextlib.suppress(OSError):
                self._io.kill()
            self._returncode = None if self._killed else code
            # Let the pumps see EOF before the handles they read from are released. A stdout pump
            # parked on a full queue (nobody reads) is cut off; what is queued stays readable.
            if self._out.full():
                self._tasks[0].cancel()
            await asyncio.wait(self._tasks, timeout=2.0)
            for task in self._tasks:
                task.cancel()
            self._out_closed = True
            if self._lifetime is not None:
                self._lifetime.cancel()
            with contextlib.suppress(Exception):
                await self._io.cleanup()
            self._exited.set()
            log.info("sandbox.process_exited", pid=self.pid, code=self._returncode)
            if self._on_exit is not None:
                try:
                    self._on_exit(self._returncode)
                except Exception as exc:  # noqa: BLE001 - a hook must not break the cleanup
                    log.warning("sandbox.on_exit_failed", error=f"{type(exc).__name__}: {exc}")

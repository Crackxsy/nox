"""`ClaudeSession`: one long-lived Claude Code process that answers turn after turn.

Starting `claude -p` for every turn cost 4-7 s before the first token: process start, login,
MCP start-up and an uncached prompt, every time. A session started with
`--input-format stream-json` stays up; each turn is one JSON line on stdin, answered by the usual
stream-json events up to a `result` line. Measured on the same machine and model: first token
about 1 s instead of 4-7 s (`spikes/sp13_native_tools.py`).

What the session owns, and nothing more:

* the process: start, one turn at a time (a lock), `interrupt` for barge-in, `kill` for the kill
  switch - the whole process tree, including the MCP bridge Claude Code started, in well under a
  second (psutil, no waiting on a graceful exit);
* what the model has already seen, so a turn only carries the history it is missing;
* the cumulative cost Claude Code reports, turned into per-turn numbers.

It never decides what to say or which tools exist; `ClaudeCodeProvider` builds the arguments and
the messages.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import psutil

from nox.ai._log import get_logger
from nox.ai.errors import ProviderError, ProviderTimeoutError, ProviderUnavailableError

log = get_logger(__name__)

PROVIDER_ID = "claude_code"
STDOUT_LINE_LIMIT = 4 * 1024 * 1024
#: How long an interrupted turn may take to wind down before the process is killed instead.
INTERRUPT_GRACE_S = 2.0
CONTROL_TIMEOUT_S = 5.0
INITIALIZE_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class SessionSpec:
    """Everything a process is started with. Two specs with the same `fingerprint` are
    interchangeable; a different one (new system prompt, model, tool set) needs a new process."""

    args: tuple[str, ...]
    env: Mapping[str, str] | None
    cwd: Path | None
    model: str

    @property
    def fingerprint(self) -> str:
        # The MCP config path changes with every spawn and is not part of the identity.
        stable = [a for a in self.args if not a.endswith(".json")]
        return hashlib.sha256("\0".join(stable).encode("utf-8")).hexdigest()[:16]


def user_line(text: str) -> bytes:
    message = {"type": "user", "message": {"role": "user", "content": text}}
    return (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")


def control_line(request_id: str, request: Mapping[str, object]) -> bytes:
    message = {"type": "control_request", "request_id": request_id, "request": dict(request)}
    return (json.dumps(message) + "\n").encode("utf-8")


def kill_tree(pid: int) -> None:
    """Kill `pid` and every descendant now (Windows does not do this for a killed parent)."""
    try:
        parent = psutil.Process(pid)
        processes = [*parent.children(recursive=True), parent]
    except psutil.NoSuchProcess:
        return
    for process in processes:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            process.kill()
    psutil.wait_procs(processes, timeout=1.0)


class ClaudeSession:
    def __init__(self, spec: SessionSpec, *, creationflags: int = 0) -> None:
        self.spec = spec
        self.model = spec.model
        self.turns = 0
        self.seen: set[str] = set()
        self.lock = asyncio.Lock()
        self._creationflags = creationflags
        self._proc: asyncio.subprocess.Process | None = None
        self._lines: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None
        self._stderr: asyncio.Task[bytes] | None = None
        self._cost_total = 0.0
        self._control = 0

    # -- lifecycle ---------------------------------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    async def start(self, executable: Sequence[str]) -> None:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *executable,
                *self.spec.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=dict(self.spec.env) if self.spec.env is not None else None,
                cwd=str(self.spec.cwd) if self.spec.cwd is not None else None,
                limit=STDOUT_LINE_LIMIT,
                creationflags=self._creationflags,
            )
        except FileNotFoundError as exc:
            raise ProviderUnavailableError(PROVIDER_ID, f"cannot start the CLI: {exc}") from exc
        except OSError as exc:
            raise ProviderError(PROVIDER_ID, f"cannot start the CLI: {exc}") from exc
        assert self._proc.stdout is not None and self._proc.stderr is not None
        self._reader = asyncio.create_task(self._read(self._proc.stdout), name="nox-claude-out")
        self._stderr = asyncio.create_task(self._proc.stderr.read(16 * 1024))
        log.info("claude_session.started", pid=self._proc.pid, model=self.model)

    async def _read(self, stdout: asyncio.StreamReader) -> None:
        try:
            while True:
                line = await stdout.readline()
                if not line:
                    break
                await self._lines.put(line)
        finally:
            await self._lines.put(None)

    def kill(self) -> None:
        """Kill the process tree immediately. Safe to call twice and from any state."""
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            kill_tree(proc.pid)
        for task in (self._reader, self._stderr):
            if task is not None and not task.done():
                task.cancel()

    async def close(self) -> None:
        """End the session politely (stdin closed), then make sure it is gone."""
        proc = self._proc
        if proc is not None and proc.stdin is not None and proc.returncode is None:
            with contextlib.suppress(OSError, RuntimeError):
                proc.stdin.close()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=3.0)
        self.kill()

    # -- turns -------------------------------------------------------------------------------------

    async def _write(self, data: bytes) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            raise ProviderError(PROVIDER_ID, "the Claude Code session has ended")
        try:
            proc.stdin.write(data)
            await proc.stdin.drain()
        except (OSError, RuntimeError) as exc:
            raise ProviderError(PROVIDER_ID, f"the Claude Code session has ended: {exc}") from exc

    async def send(self, text: str) -> None:
        await self._write(user_line(text))
        self.turns += 1

    async def next_line(self, deadline: float) -> str | None:
        """The next stdout line, `None` when the process ended, or a timeout error."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderTimeoutError(PROVIDER_ID, "turn timed out")
        try:
            raw = await asyncio.wait_for(self._lines.get(), timeout=remaining)
        except TimeoutError as exc:
            raise ProviderTimeoutError(PROVIDER_ID, "turn timed out") from exc
        return raw.decode("utf-8", errors="replace") if raw is not None else None

    async def drain_until_result(self, timeout_s: float) -> bool:
        """Read and drop lines until a `result` line; False if the process ended or time ran out."""
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                line = await self.next_line(deadline)
            except ProviderTimeoutError:
                return False
            if line is None:
                return False
            if '"type":"result"' in line.replace(" ", ""):
                return True

    def cost_delta(self, cumulative: float | None) -> float | None:
        """Claude Code reports the session's running total; a turn costs the difference."""
        if cumulative is None:
            return None
        delta = max(0.0, cumulative - self._cost_total)
        self._cost_total = cumulative
        return delta

    def request_interrupt(self) -> None:
        """Ask the CLI to stop the current answer, without waiting (usable while cancelling).
        The line is flushed by the next write; the next turn drains the rest of the answer."""
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            return
        self._control += 1
        with contextlib.suppress(OSError, RuntimeError):
            proc.stdin.write(control_line(f"nox-{self._control}", {"subtype": "interrupt"}))

    async def interrupt(self) -> bool:
        """Stop the current answer and wait for it to wind down (barge-in). False = kill it."""
        self._control += 1
        try:
            await self._write(control_line(f"nox-{self._control}", {"subtype": "interrupt"}))
        except ProviderError:
            return False
        return await self.drain_until_result(INTERRUPT_GRACE_S)

    async def initialize(self) -> None:
        """Let the CLI start up now - login, MCP servers (the bridge connects and lists tools) -
        instead of on the first message. Costs no model call; measured ~2.7 s."""
        self._control += 1
        request_id = f"nox-{self._control}"
        await self._write(control_line(request_id, {"subtype": "initialize"}))
        deadline = time.monotonic() + INITIALIZE_TIMEOUT_S
        while True:
            line = await self.next_line(deadline)
            if line is None:
                raise ProviderError(PROVIDER_ID, "the Claude Code session ended while starting")
            if request_id in line and "control_response" in line:
                return

    async def set_model(self, model: str) -> None:
        """Switch the model for the following turns without restarting the process."""
        if model == self.model:
            return
        self._control += 1
        request_id = f"nox-{self._control}"
        await self._write(control_line(request_id, {"subtype": "set_model", "model": model}))
        deadline = time.monotonic() + CONTROL_TIMEOUT_S
        while True:
            line = await self.next_line(deadline)
            if line is None:
                raise ProviderError(PROVIDER_ID, "the Claude Code session ended on a model switch")
            if request_id in line and "control_response" in line:
                if '"error"' in line:
                    raise ProviderError(PROVIDER_ID, f"model switch to {model} refused")
                self.model = model
                return

"""nox-supervisor: independent watchdog + kill switch (Process Model §Supervisor, ADR-002, P1).

Spawns core (`supervisor.core_command`) and shell, listens on the localhost control port
(`supervisor.control_port`, token in `paths.runtime_dir/supervisor.token`, also passed to children
via NOX_SUPERVISOR_* env vars - never on the command line), and applies the rules: 5 missed
heartbeats -> graceful restart request (`sup.kill` mode=restart, a clean shutdown the supervisor
answers by respawning - never the kill switch), 10 -> hard restart (psutil tree kill), more than
`restart_limit` restarts per `restart_window_s` -> safe mode. Missed-heartbeat accounting only
starts `boot_grace_s` (default 90 s) after spawn: a cold boot legitimately takes tens of seconds,
and counting from spawn time restarted cores that were still importing. Inside the grace only the
hard liveness check applies (the process exited -> restart). Kill switch (global
hotkey via optional pynput, tray/shell over the channel): `sup.kill` to core, 2 s ack window, else
terminate the tree and relaunch the shell with NOX_SAFE_MODE=1. Graceful shutdown (B-6, tray "Quit"
or any authenticated client): `sup.stop` asks the supervisor to stop core, shell and itself - the
core is told `sup.stop {reason}` and given `supervisor.stop_timeout_s` (default 6 s) to run
`NoxCore.stop()` and exit 0 on its own before the supervisor terminates it via the job object.
Every connection authenticates once (B-1): the first frame must be `sup.auth {token, role, pid}`;
frames sent before that succeeds are dropped and logged once per connection, a wrong token closes
the connection, and no frame after `sup.auth_ok` carries a token. All children live in one Windows
job object so nothing outlives the supervisor. The supervisor never touches AI, network or the
database.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

import psutil
from pydantic import BaseModel, ConfigDict, Field

from nox.core.config import NoxConfig, load_config
from nox.core.jobobject import JobObject
from nox.core.logging import configure_logging, get_logger, shutdown_logging
from nox.ipc.protocol import Envelope, Kind, Source
from nox.ipc.tokens import constant_time_equals, generate_token, write_secret_file
from nox.supervisor import messages as m

SRC = Source(role="supervisor", id="supervisor")

#: Seconds after spawning the core during which only the hard liveness check applies. The
#: config key `supervisor.boot_grace_s` overrides it once `NoxConfig` carries the field
#: (`SupervisorConfig` is owned elsewhere); until then this is the effective default.
DEFAULT_BOOT_GRACE_S = 90.0


class SupervisorState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    RESTARTING = "restarting"
    SAFE_MODE = "safe_mode"
    STOPPED = "stopped"


class SupervisorSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    control_host: str = "127.0.0.1"
    control_port: int = 47799
    runtime_dir: Path
    core_command: list[str]
    shell_command: list[str] | None = None
    heartbeat_interval_s: float = 2.0
    missed_for_graceful: int = 5
    missed_for_hard: int = 10
    boot_grace_s: float = DEFAULT_BOOT_GRACE_S
    restart_limit: int = 3
    restart_window_s: float = 300.0
    kill_ack_timeout_s: float = 2.0
    stop_timeout_s: float = 6.0
    shutdown_grace_s: float = 10.0
    kill_switch_hotkey: str | None = "ctrl+alt+shift+k"
    hotkey_enabled: bool = True
    use_job_object: bool = True
    extra_env: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg: NoxConfig, *, hotkey_enabled: bool = True) -> SupervisorSettings:
        sup = cfg.supervisor
        return cls(
            control_host=sup.control_host,
            control_port=sup.control_port,
            runtime_dir=cfg.paths.runtime_dir,
            core_command=resolve_command(sup.core_command),
            shell_command=resolve_command(sup.shell_command) if sup.shell_enabled else None,
            heartbeat_interval_s=sup.heartbeat_interval_s,
            missed_for_graceful=sup.missed_for_graceful,
            missed_for_hard=sup.missed_for_hard,
            boot_grace_s=float(getattr(sup, "boot_grace_s", DEFAULT_BOOT_GRACE_S)),
            restart_limit=sup.restart_limit,
            restart_window_s=sup.restart_window_s,
            kill_ack_timeout_s=sup.kill_ack_timeout_s,
            stop_timeout_s=sup.stop_timeout_s,
            kill_switch_hotkey=sup.kill_switch_hotkey or None,
            hotkey_enabled=hotkey_enabled,
        )


def resolve_command(command: list[str]) -> list[str]:
    """`python` means the interpreter running the supervisor (same venv)."""
    if command and command[0] in ("python", "python3", "python.exe"):
        return [sys.executable, *command[1:]]
    return list(command)


class SupervisorStatus(BaseModel):
    state: SupervisorState
    core_pid: int | None = None
    shell_pid: int | None = None
    core_connected: bool = False
    heartbeat_age_s: float | None = None
    missed_heartbeats: int = 0
    restarts_in_window: int = 0
    safe_mode_reason: str = ""
    kill_switch_engaged: bool = False


class _Child:
    def __init__(self, name: str, proc: subprocess.Popen[bytes]) -> None:
        self.name = name
        self.proc = proc
        self.started_at = time.monotonic()

    @property
    def pid(self) -> int:
        return self.proc.pid

    def alive(self) -> bool:
        return self.proc.poll() is None


def kill_process_tree(pid: int, *, grace_s: float = 1.0) -> list[int]:
    """Terminate `pid` and all descendants (psutil); returns the pids that were killed."""
    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return []
    procs = [*root.children(recursive=True), root]
    for proc in procs:
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(procs, timeout=grace_s)
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(alive, timeout=grace_s)
    return [p.pid for p in procs]


class Supervisor:
    def __init__(
        self, settings: SupervisorSettings, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._s = settings
        self._clock = clock
        self._log = get_logger(__name__)
        self._token = generate_token()
        self._server: asyncio.AbstractServer | None = None
        self._job = JobObject("nox-supervisor") if settings.use_job_object else None
        self._core: _Child | None = None
        self._shell: _Child | None = None
        self._core_writer: asyncio.StreamWriter | None = None
        self._last_heartbeat: float | None = None
        self._core_spawned_at: float = clock()
        self._core_ready = False  # authenticated *and* first heartbeat seen
        self._graceful_requested = False
        self._restarts: deque[float] = deque()
        self._shell_restarts: deque[float] = deque()
        self._state = SupervisorState.STARTING
        self._safe_mode_reason = ""
        self._kill_engaged = False
        self._pending_acks: dict[str, asyncio.Future[Envelope]] = {}
        self._watch_task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._hotkey: Any = None
        self._lock = asyncio.Lock()
        self._bg: set[asyncio.Task[None]] = set()

    # ---- public API --------------------------------------------------------------------------

    @property
    def state(self) -> SupervisorState:
        return self._state

    @property
    def port(self) -> int:
        return self._s.control_port

    @property
    def token(self) -> str:
        return self._token

    def status(self) -> SupervisorStatus:
        now = self._clock()
        age = None if self._last_heartbeat is None else now - self._last_heartbeat
        self._prune_restarts(now)
        return SupervisorStatus(
            state=self._state,
            core_pid=self._core.pid if self._core and self._core.alive() else None,
            shell_pid=self._shell.pid if self._shell and self._shell.alive() else None,
            core_connected=self._core_writer is not None,
            heartbeat_age_s=age,
            missed_heartbeats=self._missed(now),
            restarts_in_window=len(self._restarts),
            safe_mode_reason=self._safe_mode_reason,
            kill_switch_engaged=self._kill_engaged,
        )

    async def start(self) -> None:
        self._s.runtime_dir.mkdir(parents=True, exist_ok=True)
        write_secret_file(m.token_path(self._s.runtime_dir), self._token)
        self._server = await asyncio.start_server(
            self._on_connection, self._s.control_host, self._s.control_port, limit=m.MAX_LINE
        )
        self._start_hotkey()
        self._spawn_core()
        if self._s.shell_command:
            self._spawn_shell(safe_mode=False)
        self._state = SupervisorState.RUNNING
        self._watch_task = asyncio.create_task(self._watch(), name="nox-supervisor-watch")
        self._log.info(
            "supervisor.started",
            port=self._s.control_port,
            core_pid=self._core.pid if self._core else None,
            job_object=bool(self._job and self._job.available),
        )

    async def run(self) -> None:
        await self.start()
        try:
            await self._stopped.wait()
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Orderly shutdown: ask core to stop, wait, then kill everything; close job and server."""
        if self._state == SupervisorState.STOPPED:
            return
        self._state = SupervisorState.STOPPED
        self._stopped.set()
        if self._watch_task is not None:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
        self._stop_hotkey()
        if self._core and self._core.alive():
            exited = await self._send_stop_and_wait(reason="supervisor_stop")
            if not exited and self._core.alive():
                self._log.error("supervisor.stop_timeout", timeout_s=self._s.stop_timeout_s)
        for child in (self._core, self._shell):
            if child is not None and child.alive():
                kill_process_tree(child.pid)
        if self._job is not None:
            self._job.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        await self._close_core_writer()
        m.token_path(self._s.runtime_dir).unlink(missing_ok=True)
        self._log.info("supervisor.stopped")

    def request_stop(self) -> None:
        self._stopped.set()

    async def kill_switch(self, *, by: str, reason: str = "") -> bool:
        """P1 path. Returns True when the core acknowledged, False when it had to be terminated."""
        async with self._lock:
            self._log.warning("supervisor.kill_switch", by=by, reason=reason)
            self._kill_engaged = True
            self._enter_safe_mode(f"kill switch by {by}: {reason}".strip())
            acked = await self._send_kill_and_wait(
                reason=reason or "kill_switch", mode="safe_mode", by=by
            )
            if acked:
                return True
            self._log.error("supervisor.kill_not_acked", timeout_s=self._s.kill_ack_timeout_s)
            for child in (self._core, self._shell):
                if child is not None and child.alive():
                    kill_process_tree(child.pid)
            self._core = None
            await self._close_core_writer()
            if self._s.shell_command:
                self._spawn_shell(safe_mode=True)
            return False

    async def resume(self, *, by: str) -> bool:
        """Leave safe mode on explicit user action: restart the core (and a non-safe-mode shell)."""
        async with self._lock:
            if self._state != SupervisorState.SAFE_MODE:
                return False
            self._log.info("supervisor.resume", by=by)
            self._kill_engaged = False
            self._safe_mode_reason = ""
            self._restarts.clear()
            if self._core is not None and self._core.alive():
                kill_process_tree(self._core.pid)
            await self._close_core_writer()
            self._spawn_core()
            if self._s.shell_command:
                if self._shell is not None and self._shell.alive():
                    kill_process_tree(self._shell.pid)
                self._spawn_shell(safe_mode=False)
            self._state = SupervisorState.RUNNING
            return True

    async def graceful_stop(self, *, reason: str = "", by: str = "shell") -> bool:
        """B-6: `sup.stop` from an authenticated client. Core, shell and the supervisor all stop.

        Sends `sup.stop {reason}` to the core and gives it `stop_timeout_s` to run `NoxCore.stop()`
        and exit 0 on its own; a core that misses the budget is terminated via the job object.
        Returns True when the core exited on its own, False when it had to be terminated.
        """
        async with self._lock:
            self._log.warning("supervisor.graceful_stop", by=by, reason=reason)
            exited = await self._send_stop_and_wait(reason=reason or "shutdown")
            if not exited and self._core is not None and self._core.alive():
                self._log.error("supervisor.stop_timeout", timeout_s=self._s.stop_timeout_s)
                kill_process_tree(self._core.pid)
            self._core = None
            await self._close_core_writer()
            if self._shell is not None and self._shell.alive():
                kill_process_tree(self._shell.pid)
            self._shell = None
            self.request_stop()
            return exited

    # ---- children ----------------------------------------------------------------------------

    def _child_env(self, *, safe_mode: bool = False) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self._s.extra_env)
        env[m.ENV_HOST] = self._s.control_host
        env[m.ENV_PORT] = str(self._s.control_port)
        env[m.ENV_TOKEN] = self._token
        if safe_mode:
            env[m.ENV_SAFE_MODE] = "1"
        else:
            env.pop(m.ENV_SAFE_MODE, None)
        return env

    def _spawn(self, name: str, command: list[str], *, safe_mode: bool = False) -> _Child | None:
        try:
            proc: subprocess.Popen[bytes] = subprocess.Popen(  # noqa: S603 - fixed argv from config
                command, env=self._child_env(safe_mode=safe_mode)
            )
        except OSError as exc:
            self._log.error("supervisor.spawn_failed", child=name, error=str(exc))
            return None
        if self._job is not None and self._job.available:
            self._job.assign(proc.pid)
        self._log.info("supervisor.spawned", child=name, pid=proc.pid, safe_mode=safe_mode)
        return _Child(name, proc)

    def _spawn_core(self) -> None:
        self._core = self._spawn("core", self._s.core_command)
        self._core_spawned_at = self._clock()
        self._last_heartbeat = None  # nothing is counted before the first real heartbeat
        self._core_ready = False
        self._graceful_requested = False

    def _spawn_shell(self, *, safe_mode: bool) -> None:
        assert self._s.shell_command is not None
        self._shell = self._spawn("shell", self._s.shell_command, safe_mode=safe_mode)

    async def _wait_exit(self, child: _Child, timeout_s: float) -> bool:
        deadline = self._clock() + timeout_s
        while child.alive() and self._clock() < deadline:  # noqa: ASYNC110 - polling a subprocess
            await asyncio.sleep(0.05)
        return not child.alive()

    # ---- watchdog ----------------------------------------------------------------------------

    def _missed(self, now: float) -> int:
        """Missed heartbeats, counted from the first heartbeat the core sent - and never before
        `boot_grace_s` has passed since it was spawned. A cold boot takes tens of seconds (imports,
        vault index), so counting from spawn time restarted a core that was merely still booting.
        The grace only delays accounting: a core that never reports is counted from the grace's end
        and is restarted like any other silent core."""
        accounting_from = self._core_spawned_at + self._s.boot_grace_s
        if now < accounting_from:
            return 0
        reference = self._last_heartbeat if self._last_heartbeat is not None else accounting_from
        return int((now - reference) // self._s.heartbeat_interval_s)

    def _prune_restarts(self, now: float) -> None:
        for window in (self._restarts, self._shell_restarts):
            while window and now - window[0] > self._s.restart_window_s:
                window.popleft()

    async def _watch(self) -> None:
        tick = min(self._s.heartbeat_interval_s / 2, 0.5)
        while True:
            await asyncio.sleep(tick)
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the watchdog must survive anything
                self._log.error("supervisor.watch_failed", error=f"{type(exc).__name__}: {exc}")

    async def _tick(self) -> None:
        if self._state in (SupervisorState.STOPPED, SupervisorState.STARTING):
            return
        now = self._clock()
        if self._shell is not None and not self._shell.alive() and self._s.shell_command:
            self._prune_restarts(now)
            if len(self._shell_restarts) < self._s.restart_limit:
                self._shell_restarts.append(now)
                self._log.warning("supervisor.shell_exited", code=self._shell.proc.returncode)
                self._spawn_shell(safe_mode=self._state == SupervisorState.SAFE_MODE)
        if self._state == SupervisorState.SAFE_MODE:
            return
        async with self._lock:
            if self._state != SupervisorState.RUNNING:
                return
            if self._core is None or not self._core.alive():
                code = None if self._core is None else self._core.proc.returncode
                await self._restart_core(reason=f"core exited (code {code})", hard=False)
                return
            missed = self._missed(now)
            if missed >= self._s.missed_for_hard:
                await self._restart_core(reason=f"{missed} heartbeats missed", hard=True)
            elif missed >= self._s.missed_for_graceful and not self._graceful_requested:
                self._graceful_requested = True
                self._log.warning("supervisor.graceful_restart_requested", missed=missed)
                acked = await self._send_kill_and_wait(reason="heartbeats missed", mode="restart")
                if acked:
                    await self._wait_exit(self._core, self._s.shutdown_grace_s)
                    await self._restart_core(reason="graceful restart", hard=False)

    async def _restart_core(self, *, reason: str, hard: bool) -> None:
        now = self._clock()
        self._prune_restarts(now)
        self._restarts.append(now)
        self._log.warning(
            "supervisor.core_restart", reason=reason, hard=hard, count=len(self._restarts)
        )
        if self._core is not None and self._core.alive():
            kill_process_tree(self._core.pid)
        self._core = None
        await self._close_core_writer()
        if len(self._restarts) > self._s.restart_limit:
            self._enter_safe_mode(f"restart limit exceeded ({reason})")
            if self._s.shell_command and self._shell is not None and self._shell.alive():
                kill_process_tree(self._shell.pid)
            if self._s.shell_command:
                self._spawn_shell(safe_mode=True)
            return
        self._state = SupervisorState.RESTARTING
        self._spawn_core()
        self._state = SupervisorState.RUNNING

    def _enter_safe_mode(self, reason: str) -> None:
        self._state = SupervisorState.SAFE_MODE
        self._safe_mode_reason = reason
        self._log.error("supervisor.safe_mode", reason=reason)

    # ---- control channel ---------------------------------------------------------------------

    async def _on_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """B-1: the first frame must be `sup.auth`; every frame after that carries no token."""
        peer = writer.get_extra_info("peername")
        is_core = False
        authed = False
        warned_unauth = False
        try:
            while True:
                try:
                    envelope = await m.read_envelope(reader)
                except m.ProtocolError as exc:
                    await self._reply_error(writer, None, "validation.failed", str(exc))
                    break
                if envelope is None:
                    break
                if not authed:
                    if envelope.name != m.NAME_AUTH:
                        if not warned_unauth:
                            self._log.warning(
                                "supervisor.unauthenticated_frame",
                                peer=str(peer),
                                name=envelope.name,
                            )
                            warned_unauth = True
                        continue
                    token = envelope.payload.get("token")
                    if not isinstance(token, str) or not constant_time_equals(token, self._token):
                        self._log.warning("supervisor.auth_failed", peer=str(peer))
                        await self._reply_error(writer, envelope, "auth.denied", "invalid token")
                        break
                    authed = True
                    role = str(envelope.payload.get("role") or envelope.src.role)
                    await self._send(writer, envelope.reply(m.NAME_AUTH_OK, {"ok": True}, SRC))
                    self._log.info("supervisor.client_authed", peer=str(peer), role=role)
                    if role == "core" and not is_core:
                        is_core = True
                        await self._close_core_writer()
                        self._core_writer = writer
                        self._log.info("supervisor.core_connected", peer=str(peer))
                    continue
                await self._handle(envelope, writer)
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            if is_core and self._core_writer is writer:
                self._core_writer = None
                self._log.info("supervisor.core_disconnected")
            try:
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass

    async def _handle(self, envelope: Envelope, writer: asyncio.StreamWriter) -> None:
        name = envelope.name
        if name == m.NAME_HEARTBEAT:
            now = self._clock()
            if not self._core_ready:
                self._core_ready = True
                self._log.info(
                    "supervisor.core_ready", boot_s=round(now - self._core_spawned_at, 1)
                )
            self._last_heartbeat = now
            self._graceful_requested = False
        elif name == m.NAME_ACK:
            fut = self._pending_acks.pop(envelope.corr or "", None)
            if fut is not None and not fut.done():
                fut.set_result(envelope)
        elif name == m.NAME_KILL:
            reason = str(envelope.payload.get("reason", ""))
            by = str(
                envelope.payload.get("origin") or envelope.payload.get("by") or envelope.src.role
            )
            await self._send(
                writer, envelope.reply(m.NAME_ACK, {"ok": True, "accepted": True}, SRC)
            )
            self._spawn_bg(self.kill_switch(by=by, reason=reason))
        elif name == m.NAME_STATUS:
            await self._send(
                writer, envelope.reply(m.NAME_STATUS, self.status().model_dump(mode="json"), SRC)
            )
        elif name == m.NAME_RESUME:
            ok = await self.resume(by=str(envelope.payload.get("by") or envelope.src.role))
            await self._send(writer, envelope.reply(m.NAME_ACK, {"ok": ok}, SRC))
        elif name == m.NAME_STOP:
            reason = str(envelope.payload.get("reason", ""))
            by = str(envelope.payload.get("origin") or envelope.src.role)
            await self._send(
                writer, envelope.reply(m.NAME_ACK, {"ok": True, "accepted": True}, SRC)
            )
            self._spawn_bg(self.graceful_stop(by=by, reason=reason))
        else:
            await self._reply_error(writer, envelope, "not_found", f"unknown message {name}")

    def _spawn_bg(self, coro: Any) -> None:
        task: asyncio.Task[None] = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _send_stop_and_wait(self, *, reason: str) -> bool:
        """Send `sup.stop` to the connected core and wait `stop_timeout_s` for it to exit on its
        own (B-6). True when it did; False when the caller must terminate it."""
        writer, core = self._core_writer, self._core
        if core is None or not core.alive():
            return True
        if writer is None:
            return False
        request = m.make(m.NAME_STOP, {"reason": reason}, SRC, kind=Kind.EVENT)
        try:
            await self._send(writer, request)
        except OSError:
            return not core.alive()
        return await self._wait_exit(core, self._s.stop_timeout_s)

    async def _send_kill_and_wait(self, *, reason: str, mode: str, by: str = "supervisor") -> bool:
        """Send `sup.kill` to the connected core and wait `kill_ack_timeout_s` for `sup.ack`."""
        writer = self._core_writer
        if writer is None:
            return False
        request = m.make(
            m.NAME_KILL, {"reason": reason, "mode": mode, "by": by}, SRC, kind=Kind.REQUEST
        )
        fut: asyncio.Future[Envelope] = asyncio.get_running_loop().create_future()
        self._pending_acks[request.id] = fut
        try:
            await self._send(writer, request)
            await asyncio.wait_for(fut, self._s.kill_ack_timeout_s)
        except (TimeoutError, OSError):
            return False
        finally:
            self._pending_acks.pop(request.id, None)
        return True

    async def _send(self, writer: asyncio.StreamWriter, envelope: Envelope) -> None:
        try:
            writer.write(m.encode(envelope))
            await writer.drain()
        except (OSError, ConnectionError) as exc:
            self._log.debug("supervisor.send_failed", name=envelope.name, error=str(exc))
            raise OSError(str(exc)) from exc

    async def _reply_error(
        self, writer: asyncio.StreamWriter, envelope: Envelope | None, code: str, message: str
    ) -> None:
        payload = {"code": code, "message": message}
        reply = (
            envelope.reply(m.NAME_ERROR, payload, SRC, kind=Kind.ERROR)
            if envelope is not None
            else m.make(m.NAME_ERROR, payload, SRC, kind=Kind.ERROR)
        )
        try:
            await self._send(writer, reply)
        except OSError:
            pass

    async def _close_core_writer(self) -> None:
        writer, self._core_writer = self._core_writer, None
        if writer is None:
            return
        try:
            writer.close()
            await writer.wait_closed()
        except OSError:
            pass

    # ---- hotkey ------------------------------------------------------------------------------

    def _start_hotkey(self) -> None:
        if not self._s.hotkey_enabled or not self._s.kill_switch_hotkey:
            return
        try:
            from pynput import keyboard  # noqa: PLC0415 - optional dependency
        except ImportError:
            self._log.warning("supervisor.hotkey_unavailable", reason="pynput not installed")
            return
        loop = asyncio.get_running_loop()
        combo = hotkey_to_pynput(self._s.kill_switch_hotkey)

        def _fire() -> None:
            loop.call_soon_threadsafe(
                lambda: self._spawn_bg(self.kill_switch(by="hotkey", reason="global hotkey"))
            )

        try:
            self._hotkey = keyboard.GlobalHotKeys({combo: _fire})
            self._hotkey.start()
            self._log.info("supervisor.hotkey_registered", hotkey=combo)
        except Exception as exc:  # noqa: BLE001 - hotkey failure never blocks the watchdog
            self._log.warning("supervisor.hotkey_failed", error=str(exc))
            self._hotkey = None

    def _stop_hotkey(self) -> None:
        if self._hotkey is not None:
            try:
                self._hotkey.stop()
            except Exception:  # noqa: BLE001, S110 - best effort
                pass
            self._hotkey = None


def hotkey_to_pynput(hotkey: str) -> str:
    """`ctrl+alt+shift+k` -> `<ctrl>+<alt>+<shift>+k` (pynput GlobalHotKeys syntax)."""
    modifiers = {"ctrl", "control", "alt", "shift", "cmd", "win", "super"}
    parts = []
    for raw in hotkey.lower().split("+"):
        key = raw.strip()
        if key in ("control",):
            key = "ctrl"
        if key in ("win", "super"):
            key = "cmd"
        parts.append(f"<{key}>" if key in modifiers or len(key) > 1 else key)
    return "+".join(parts)


# ---- entry point ---------------------------------------------------------------------------------


def default_config_paths() -> tuple[Path, Path | None]:
    """(defaults.yaml, user.yaml or None). NOX_CONFIG_DEFAULTS / NOX_USER_CONFIG override."""
    env_defaults = os.environ.get("NOX_CONFIG_DEFAULTS")
    defaults = (
        Path(env_defaults)
        if env_defaults
        else Path(__file__).resolve().parents[3] / "config" / "defaults.yaml"
    )
    env_user = os.environ.get("NOX_USER_CONFIG")
    if env_user:
        return defaults, Path(env_user)
    appdata = os.environ.get("APPDATA")
    candidates = []
    if appdata:
        candidates.append(Path(appdata) / "Nox" / "user.yaml")
    candidates.append(defaults.parent / "user.yaml")
    for candidate in candidates:
        if candidate.is_file():
            return defaults, candidate
    return defaults, None


def main(argv: list[str] | None = None) -> int:
    """Console entry (`python -m nox.supervisor`, `nox supervisor`). Returns the exit code."""
    defaults, user = default_config_paths()
    cfg = load_config(defaults, user)
    configure_logging(
        cfg.paths.logs_dir,
        level=cfg.logging.level,
        json_file=cfg.logging.json_output,
        pii=cfg.logging.pii_filter,
        retention_days=cfg.log_retention_days,
        filename="supervisor.log",
    )
    log = get_logger(__name__)
    for warning in cfg.warnings:
        log.warning(
            "config.warning", layer=warning.layer, source=warning.source, message=warning.message
        )
    settings = SupervisorSettings.from_config(cfg)
    supervisor = Supervisor(settings)
    try:
        asyncio.run(supervisor.run())
    except KeyboardInterrupt:
        return 130
    finally:
        shutdown_logging()
    return 0

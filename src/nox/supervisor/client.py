"""SupervisorClient: the core's side of the control channel (Runtime Lifecycle step 8).

Authenticates once per connection (B-1): sends `sup.auth {token, role, pid, core_secret}` as the
first frame - the secret is the one the supervisor generated for this spawn, which is what makes
the supervisor accept this connection as the core - and waits for `sup.auth_ok` before doing
anything else; every frame after that (`sup.heartbeat`,
`sup.ack`) carries no token. Sends `sup.heartbeat` every `interval_s` and answers `sup.kill` with
`sup.ack` immediately - the ack means "received and acting", which is what the supervisor's 2 s
window measures - then dispatches on the frame's `mode`: `mode=restart` (the watchdog's graceful
restart request) calls the synchronous `on_restart` hook, which signals a clean shutdown so the
supervisor respawns the core; every other mode (`safe_mode`, panic, the kill switch) runs the
injected `on_kill` coroutine (entering SAFE_MODE, stopping workers) as a task. A restart request is
not a kill - engaging the kill switch for one left Nox mute in safe mode with nothing restarted
(the product owner's log, 2026-09-15).
`sup.stop {reason}` (B-6, supervisor -> core) calls the synchronous `on_stop`
hook, which only needs to signal the core's own shutdown path - `NoxCore.stop()` runs from the
process's normal run loop, not from here, so no ack is sent or expected. Reconnects with backoff;
when no supervisor answers the core keeps running standalone (dev mode) and logs a warning once.

`notify_resumed` is the one request the core sends: `sup.resume {by, rearm: true}` after the user
left safe mode through `security.resume` (PIN-checked there). A kill from the hotkey or the tray
puts the supervisor into safe mode as well, where its watchdog restarts nothing; this is what takes
it out again, without restarting the core that just resumed.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import Any

from nox.core.logging import get_logger
from nox.ipc.protocol import Envelope, Kind, Source
from nox.supervisor import messages as m

KillHandler = Callable[[str, str], Awaitable[None]]  # (reason, by)
StopHandler = Callable[[str], None]  # (reason,)
RestartHandler = Callable[[str], None]  # (reason,)
StatusProvider = Callable[[], dict[str, Any]]

#: `sup.kill` mode that asks for a clean shutdown + respawn instead of the kill switch.
MODE_RESTART = "restart"

#: How long `notify_resumed` waits for the supervisor's answer.
RESUME_ACK_TIMEOUT_S = 2.0
#: `notify_resumed` outcomes, reported in the user-facing `security.resume` response.
RESUME_REARMED = "rearmed"
RESUME_NOT_IN_SAFE_MODE = "not_in_safe_mode"
RESUME_UNREACHABLE = "unreachable"


class AuthError(RuntimeError):
    """The supervisor rejected the connection's `sup.auth` frame."""


class SupervisorClient:
    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        *,
        on_kill: KillHandler | None = None,
        on_stop: StopHandler | None = None,
        on_restart: RestartHandler | None = None,
        status_provider: StatusProvider | None = None,
        interval_s: float = 2.0,
        reconnect_delay_s: float = 1.0,
        max_reconnect_delay_s: float = 10.0,
        client_id: str = "core",
        pid: int | None = None,
        core_secret: str | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._token = token
        self._on_kill = on_kill
        self._on_stop = on_stop
        self._on_restart = on_restart
        self._status_provider = status_provider
        self._interval = interval_s
        self._reconnect_delay = reconnect_delay_s
        self._max_reconnect_delay = max_reconnect_delay_s
        self._src = Source(role="core", id=client_id)
        self._pid = pid or os.getpid()
        self._core_secret = core_secret
        self._log = get_logger(__name__)
        self._task: asyncio.Task[None] | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = asyncio.Event()
        self._standalone_warned = False
        self.heartbeats_sent = 0
        self.kills_received = 0
        self.restarts_requested = 0
        self._kill_tasks: set[asyncio.Task[None]] = set()
        self._pending: dict[str, asyncio.Future[Envelope]] = {}

    @classmethod
    def from_env(cls, **kwargs: Any) -> SupervisorClient | None:
        """Build from NOX_SUPERVISOR_{HOST,PORT,TOKEN,CORE_SECRET}; None when not spawned by a
        supervisor.

        The token and the spawn secret are removed from the process environment here, once read:
        every worker, plugin and tool the core starts copies the environment, and none of them may
        be able to talk to the supervisor, least of all as the core.
        """
        settings = m.env_settings()
        if settings is None:
            return None
        host, port, token = settings
        core_secret = m.take_core_credentials()
        return cls(host, port, token, core_secret=core_secret, **kwargs)

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def wait_connected(self, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
        except TimeoutError:
            return False
        return True

    # ---- lifecycle ---------------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="nox-supervisor-client")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._close_writer()

    async def _run(self) -> None:
        delay = self._reconnect_delay
        while True:
            try:
                reader, writer = await asyncio.open_connection(self._host, self._port)
            except OSError as exc:
                if not self._standalone_warned:
                    self._log.warning(
                        "supervisor.unreachable",
                        host=self._host,
                        port=self._port,
                        error=str(exc),
                        note="running standalone until the supervisor answers",
                    )
                    self._standalone_warned = True
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_reconnect_delay)
                continue
            delay = self._reconnect_delay
            self._writer = writer
            try:
                await self._authenticate(reader, writer)
            except (OSError, AuthError) as exc:
                self._log.warning("supervisor.auth_failed", error=str(exc))
                await self._close_writer()
                await asyncio.sleep(self._reconnect_delay)
                continue
            self._connected.set()
            self._log.info("supervisor.connected", host=self._host, port=self._port)
            try:
                await self._session(reader, writer)
            except asyncio.CancelledError:
                raise
            except (OSError, m.ProtocolError) as exc:
                self._log.warning("supervisor.connection_lost", error=str(exc))
            finally:
                self._connected.clear()
                await self._close_writer()
            await asyncio.sleep(self._reconnect_delay)

    async def _session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        heartbeat = asyncio.create_task(self._heartbeat_loop(writer), name="nox-heartbeat")
        try:
            while True:
                envelope = await m.read_envelope(reader)
                if envelope is None:
                    return
                await self._handle(envelope, writer)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    async def _authenticate(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """B-1: send `sup.auth` first and require `sup.auth_ok` before anything else."""
        await self._send(
            writer,
            m.auth_frame(
                self._token,
                role="core",
                pid=self._pid,
                src=self._src,
                core_secret=self._core_secret,
            ),
        )
        reply = await m.read_envelope(reader)
        if reply is None or reply.name != m.NAME_AUTH_OK:
            detail = reply.payload if reply is not None else "connection closed"
            raise AuthError(f"supervisor rejected sup.auth: {detail}")

    async def _heartbeat_loop(self, writer: asyncio.StreamWriter) -> None:
        while True:
            payload: dict[str, Any] = {"pid": self._pid}
            if self._status_provider is not None:
                try:
                    payload.update(self._status_provider())
                except Exception as exc:  # noqa: BLE001 - status is decoration; heartbeats must go out
                    payload["status_error"] = str(exc)
            await self._send(writer, m.make(m.NAME_HEARTBEAT, payload, self._src))
            self.heartbeats_sent += 1
            await asyncio.sleep(self._interval)

    async def send_heartbeat_now(self) -> bool:
        if self._writer is None:
            return False
        await self._send(
            self._writer,
            m.make(m.NAME_HEARTBEAT, {"pid": self._pid}, self._src),
        )
        self.heartbeats_sent += 1
        return True

    async def notify_resumed(self, *, by: str, timeout_s: float = RESUME_ACK_TIMEOUT_S) -> str:
        """Tell the supervisor the user resumed, so it leaves safe mode and watches again.

        Returns `rearmed`, `not_in_safe_mode` (it was not in safe mode - a kill from the dashboard
        never reached it) or `unreachable`. Never raises: the core has resumed either way, and the
        answer is reported, not acted on.
        """
        writer = self._writer
        if writer is None or not self.connected:
            self._log.warning("supervisor.resume_not_delivered", reason="not connected", by=by)
            return RESUME_UNREACHABLE
        request = m.make(m.NAME_RESUME, {"by": by, "rearm": True}, self._src, kind=Kind.REQUEST)
        reply: asyncio.Future[Envelope] = asyncio.get_running_loop().create_future()
        self._pending[request.id] = reply
        try:
            await self._send(writer, request)
            answer = await asyncio.wait_for(reply, timeout_s)
        except (TimeoutError, OSError) as exc:
            self._log.warning("supervisor.resume_not_delivered", reason=type(exc).__name__, by=by)
            return RESUME_UNREACHABLE
        finally:
            self._pending.pop(request.id, None)
        if answer.payload.get("ok") is True:
            self._log.info("supervisor.rearmed", by=by)
            return RESUME_REARMED
        return str(answer.payload.get("reason") or RESUME_NOT_IN_SAFE_MODE)

    async def _handle(self, envelope: Envelope, writer: asyncio.StreamWriter) -> None:
        pending = self._pending.pop(envelope.corr, None) if envelope.corr else None
        if pending is not None:
            if not pending.done():
                pending.set_result(envelope)
            return
        if envelope.name == m.NAME_KILL:
            reason = str(envelope.payload.get("reason", ""))
            by = str(envelope.payload.get("by", "supervisor"))
            mode = str(envelope.payload.get("mode", "") or "safe_mode")
            restart = mode == MODE_RESTART
            if restart:
                self.restarts_requested += 1
                self._log.warning("supervisor.restart_received", reason=reason, by=by)
            else:
                self.kills_received += 1
                self._log.warning("supervisor.kill_received", reason=reason, by=by, mode=mode)
            await self._send(
                writer,
                m.make(
                    m.NAME_ACK,
                    {"ok": True, "pid": self._pid, "mode": mode},
                    self._src,
                    kind=Kind.RESPONSE,
                    corr=envelope.id,
                ),
            )
            if restart:
                # A clean shutdown (exit 0); the supervisor respawns the core. Never the kill
                # switch - that would leave the user in safe mode with nothing restarted.
                if self._on_restart is not None:
                    self._on_restart(reason)
                else:
                    self._log.warning("supervisor.restart_unhandled", reason=reason)
            elif self._on_kill is not None:
                task = asyncio.create_task(self._run_kill(reason, by), name="nox-kill-handler")
                self._kill_tasks.add(task)
                task.add_done_callback(self._kill_tasks.discard)
        elif envelope.name == m.NAME_STOP:
            reason = str(envelope.payload.get("reason", ""))
            self._log.warning("supervisor.stop_received", reason=reason)
            if self._on_stop is not None:
                self._on_stop(reason)
        elif envelope.kind == Kind.RESPONSE or envelope.name == m.NAME_ERROR:
            if envelope.name == m.NAME_ERROR:
                self._log.warning("supervisor.error", payload=envelope.payload)
        else:
            self._log.debug("supervisor.unhandled", name=envelope.name)

    async def _run_kill(self, reason: str, by: str) -> None:
        assert self._on_kill is not None
        try:
            await self._on_kill(reason, by)
        except Exception as exc:  # noqa: BLE001 - the kill handler must never crash the client
            self._log.error("supervisor.kill_handler_failed", error=f"{type(exc).__name__}: {exc}")

    async def _send(self, writer: asyncio.StreamWriter, envelope: Envelope) -> None:
        writer.write(m.encode(envelope))
        await writer.drain()

    async def _close_writer(self) -> None:
        writer, self._writer = self._writer, None
        if writer is None:
            return
        try:
            writer.close()
            await writer.wait_closed()
        except (OSError, asyncio.CancelledError):
            pass

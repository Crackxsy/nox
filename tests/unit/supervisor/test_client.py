"""nox.supervisor.client: authenticates once per connection (B-1), heartbeats/ack carry no token
afterwards, kill is acked and dispatched, sup.stop calls on_stop, standalone mode."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator

import pytest

from nox.ipc.protocol import Envelope, Kind, Source
from nox.supervisor import messages as m
from nox.supervisor.client import SupervisorClient

TOKEN = "t" * 32  # noqa: S105 - test token
SUP_SRC = Source(role="supervisor", id="s")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class FakeSupervisor:
    """Requires the B-1 handshake before treating a connection as authenticated."""

    def __init__(self, *, accept_token: str | None = TOKEN) -> None:
        self.accept_token = accept_token
        self.received: list[Envelope] = []
        self.writer: asyncio.StreamWriter | None = None
        self.got_first = asyncio.Event()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            auth = await m.read_envelope(reader)
            if auth is None or auth.name != m.NAME_AUTH:
                return
            if self.accept_token is None or auth.payload.get("token") != self.accept_token:
                writer.write(m.encode(auth.reply(m.NAME_ERROR, {"code": "auth.denied"}, SUP_SRC)))
                await writer.drain()
                return
            writer.write(m.encode(auth.reply(m.NAME_AUTH_OK, {"ok": True}, SUP_SRC)))
            await writer.drain()
            self.writer = writer
            while True:
                env = await m.read_envelope(reader)
                if env is None:
                    return
                self.received.append(env)
                self.got_first.set()
        finally:
            # Python 3.13: Server.wait_closed() waits for every accepted connection to actually
            # close, not just for the listening socket - an unclosed writer here hangs the
            # `server` fixture's teardown (and every test after it) forever.
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.CancelledError):
                pass


@pytest.fixture
async def server() -> AsyncIterator[tuple[FakeSupervisor, int]]:
    fake = FakeSupervisor()
    port = free_port()
    srv = await asyncio.start_server(fake.handle, "127.0.0.1", port)
    yield fake, port
    srv.close()
    await srv.wait_closed()


async def test_heartbeats_and_kill_ack(server: tuple[FakeSupervisor, int]) -> None:
    fake, port = server
    kills: list[tuple[str, str]] = []

    async def on_kill(reason: str, by: str) -> None:
        kills.append((reason, by))

    client = SupervisorClient(
        "127.0.0.1",
        port,
        TOKEN,
        on_kill=on_kill,
        interval_s=0.05,
        pid=4242,
        status_provider=lambda: {"level": "running"},
    )
    client.start()
    assert await client.wait_connected(2.0)
    await asyncio.wait_for(fake.got_first.wait(), 2.0)
    await asyncio.sleep(0.12)
    beats = [e for e in fake.received if e.name == m.NAME_HEARTBEAT]
    assert len(beats) >= 2
    assert "token" not in beats[0].payload  # B-1: no token outside sup.auth
    assert beats[0].payload["pid"] == 4242
    assert beats[0].payload["level"] == "running" and beats[0].src.role == "core"

    assert fake.writer is not None
    kill = m.make(
        m.NAME_KILL,
        {"reason": "test", "by": "hotkey"},
        SUP_SRC,
        kind=Kind.REQUEST,
    )
    fake.writer.write(m.encode(kill))
    await fake.writer.drain()
    for _ in range(50):
        await asyncio.sleep(0.02)
        if any(e.name == m.NAME_ACK for e in fake.received):
            break
    acks = [e for e in fake.received if e.name == m.NAME_ACK]
    assert acks and acks[0].corr == kill.id and acks[0].kind is Kind.RESPONSE
    assert "token" not in acks[0].payload  # B-1: no token outside sup.auth
    assert kills == [("test", "hotkey")]
    assert client.kills_received == 1
    await client.stop()
    assert not client.connected


async def test_sup_stop_calls_on_stop_without_ack(server: tuple[FakeSupervisor, int]) -> None:
    fake, port = server
    stops: list[str] = []
    client = SupervisorClient(
        "127.0.0.1", port, TOKEN, on_stop=lambda reason: stops.append(reason), interval_s=0.05
    )
    client.start()
    assert await client.wait_connected(2.0)
    await asyncio.wait_for(fake.got_first.wait(), 2.0)

    assert fake.writer is not None
    stop_msg = m.make(m.NAME_STOP, {"reason": "user_quit"}, SUP_SRC, kind=Kind.EVENT)
    fake.writer.write(m.encode(stop_msg))
    await fake.writer.drain()
    for _ in range(50):
        await asyncio.sleep(0.02)
        if stops:
            break
    assert stops == ["user_quit"]
    # B-6: sup.stop is not acked - NoxCore.stop() runs from the process's own run loop
    assert not any(e.name == m.NAME_ACK for e in fake.received)
    await client.stop()


async def test_wrong_token_auth_rejected() -> None:
    fake = FakeSupervisor(accept_token="a-different-token-" + "x" * 16)
    port = free_port()
    srv = await asyncio.start_server(fake.handle, "127.0.0.1", port)
    try:
        client = SupervisorClient("127.0.0.1", port, TOKEN, reconnect_delay_s=0.05)
        client.start()
        assert await client.wait_connected(0.5) is False  # auth denied, never reaches "connected"
        assert client.heartbeats_sent == 0
        await client.stop()
    finally:
        srv.close()
        await srv.wait_closed()


async def test_standalone_without_supervisor() -> None:
    client = SupervisorClient("127.0.0.1", free_port(), TOKEN, reconnect_delay_s=0.02)
    client.start()
    assert await client.wait_connected(0.1) is False
    assert client.heartbeats_sent == 0
    await client.stop()


def test_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(m.ENV_PORT, raising=False)
    monkeypatch.delenv(m.ENV_TOKEN, raising=False)
    assert SupervisorClient.from_env() is None
    monkeypatch.setenv(m.ENV_PORT, "4711")
    monkeypatch.setenv(m.ENV_TOKEN, TOKEN)
    client = SupervisorClient.from_env(interval_s=1.0)
    assert client is not None and not client.connected


async def test_kill_mode_restart_calls_on_restart_not_on_kill(
    server: tuple[FakeSupervisor, int],
) -> None:
    """`sup.kill mode=restart` is the watchdog asking for a fresh core: acked like any kill, but
    routed to the clean-shutdown hook so the supervisor can respawn - not to the kill switch."""
    fake, port = server
    kills: list[tuple[str, str]] = []
    restarts: list[str] = []

    async def on_kill(reason: str, by: str) -> None:
        kills.append((reason, by))

    client = SupervisorClient(
        "127.0.0.1",
        port,
        TOKEN,
        on_kill=on_kill,
        on_restart=restarts.append,
        interval_s=0.05,
    )
    client.start()
    assert await client.wait_connected(2.0)
    await asyncio.wait_for(fake.got_first.wait(), 2.0)

    assert fake.writer is not None
    kill = m.make(
        m.NAME_KILL,
        {"reason": "heartbeats missed", "mode": "restart", "by": "supervisor"},
        SUP_SRC,
        kind=Kind.REQUEST,
    )
    fake.writer.write(m.encode(kill))
    await fake.writer.drain()
    for _ in range(50):
        await asyncio.sleep(0.02)
        if restarts:
            break
    assert restarts == ["heartbeats missed"]
    assert kills == []  # the kill switch stays out of it
    assert client.restarts_requested == 1 and client.kills_received == 0
    acks = [e for e in fake.received if e.name == m.NAME_ACK]
    assert acks and acks[0].corr == kill.id  # the supervisor's 2 s ack window is still served
    await client.stop()


async def test_kill_without_mode_still_engages_the_kill_switch(
    server: tuple[FakeSupervisor, int],
) -> None:
    fake, port = server
    kills: list[tuple[str, str]] = []
    restarts: list[str] = []

    async def on_kill(reason: str, by: str) -> None:
        kills.append((reason, by))

    client = SupervisorClient(
        "127.0.0.1", port, TOKEN, on_kill=on_kill, on_restart=restarts.append, interval_s=0.05
    )
    client.start()
    assert await client.wait_connected(2.0)
    await asyncio.wait_for(fake.got_first.wait(), 2.0)
    assert fake.writer is not None
    fake.writer.write(
        m.encode(m.make(m.NAME_KILL, {"reason": "panic", "by": "tray"}, SUP_SRC, kind=Kind.REQUEST))
    )
    await fake.writer.drain()
    for _ in range(50):
        await asyncio.sleep(0.02)
        if kills:
            break
    assert kills == [("panic", "tray")] and restarts == []
    await client.stop()

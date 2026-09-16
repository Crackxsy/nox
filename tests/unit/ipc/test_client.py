"""IpcClient / IpcClientThread: auth errors, subscriptions, reconnect/backoff, thread runner."""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from nox.core.events import Event
from nox.core.logging import configure_logging, shutdown_logging
from nox.ipc.client import IpcClient, IpcClientThread
from nox.ipc.errors import ERR_AUTH_DENIED, ERR_INTERNAL, ERR_UNAVAILABLE, IpcError
from nox.ipc.protocol import Envelope
from nox.ipc.server import HubSettings, IpcHub
from nox.ipc.tokens import TokenStore

from .conftest import HubFactory, SimpleBus


async def test_client_auth_denied(hub: IpcHub) -> None:
    client = IpcClient(hub.url, "wrong-token-wrong-token", "shell", "shell:1")
    with pytest.raises(IpcError) as exc:
        await client.connect()
    assert exc.value.code == ERR_AUTH_DENIED
    assert not client.connected


async def test_client_unreachable(tmp_path: Path) -> None:
    client = IpcClient("ws://127.0.0.1:1/ws", "x" * 20, "shell", "shell:1")
    with pytest.raises(IpcError) as exc:
        await client.connect()
    assert exc.value.code == ERR_UNAVAILABLE and exc.value.retryable


async def test_client_request_and_events(hub: IpcHub, tokens: TokenStore, bus: SimpleBus) -> None:
    client = IpcClient(hub.url, tokens.session_token, "shell", "shell:1", client_version="0.1.0")
    events: list[Envelope] = []
    await client.subscribe(["pet.*"], events.append)
    auth = await client.connect()
    assert auth.ok and client.session_id == auth.session_id
    assert (await client.request("state.get", {"path": "a"}))["path"] == "a"
    with pytest.raises(IpcError) as exc:
        await client.request("nothing.at_all")
    assert exc.value.code == "not_found"
    remove = client.on("system.*", events.append)
    await bus.publish(Event(name="pet.state_changed", payload={"functional": "idle"}))
    await bus.publish(Event(name="system.started"))
    await asyncio.sleep(0.2)
    assert sorted(e.name for e in events) == ["pet.state_changed", "system.started"]
    remove()
    await bus.publish(Event(name="system.stopping"))
    await asyncio.sleep(0.1)
    assert len(events) == 2
    await client.close()
    with pytest.raises(IpcError):
        await client.request("state.get")


async def test_client_reconnects_after_hub_restart(
    hub_factory: HubFactory, tokens: TokenStore, registry: Any, bus: SimpleBus, runtime_dir: Path
) -> None:
    hub = await hub_factory()
    port = hub.port
    states: list[bool] = []
    client = IpcClient(
        hub.url,
        tokens.session_token,
        "shell",
        "shell:1",
        reconnect=True,
        backoff_initial_s=0.05,
        backoff_max_s=0.2,
        on_connection_change=states.append,
    )
    await client.subscribe(["pet.*"])
    await client.connect()
    assert (await client.request("health.get")) == {"components": {}}
    await hub.stop()
    await asyncio.sleep(0.1)
    assert not client.connected
    with pytest.raises(IpcError) as exc:
        await client.request("health.get")
    assert exc.value.code == ERR_UNAVAILABLE
    hub2 = IpcHub(HubSettings(port=port), tokens, registry, bus, runtime_dir)
    await hub2.start()
    try:
        for _ in range(100):
            if client.connected:
                break
            await asyncio.sleep(0.05)
        assert client.connected
        assert (await client.request("health.get")) == {"components": {}}
        # subscriptions survive the reconnect
        got: asyncio.Queue[str] = asyncio.Queue()
        client.on("pet.*", lambda e: got.put_nowait(e.name))
        await bus.publish(Event(name="pet.state_changed", payload={"functional": "idle"}))
        assert await asyncio.wait_for(got.get(), 2) == "pet.state_changed"
        assert states == [True, False, True]
    finally:
        await client.close()
        await hub2.stop()


async def test_client_reconnect_stops_on_auth_denied(
    hub_factory: HubFactory, registry: Any, bus: SimpleBus, runtime_dir: Path
) -> None:
    tokens = TokenStore()
    hub = IpcHub(HubSettings(port=0), tokens, registry, bus, runtime_dir)
    await hub.start()
    port = hub.port
    worker = IpcClient(
        hub.url,
        tokens.issue_worker_token("worker:stt"),
        "worker",
        "worker:stt",
        reconnect=True,
        backoff_initial_s=0.05,
    )
    await worker.connect()
    await hub.stop()
    hub2 = IpcHub(HubSettings(port=port), tokens, registry, bus, runtime_dir)
    await hub2.start()
    try:
        await asyncio.sleep(0.5)
        assert not worker.connected  # one-time token is consumed: reconnect is denied and stops
        assert worker._reconnector is not None and worker._reconnector.done()
    finally:
        await worker.close()
        await hub2.stop()


def _run_hub_in_thread(
    tokens: TokenStore, registry: Any, runtime_dir: Path
) -> tuple[IpcHub, SimpleBus, asyncio.AbstractEventLoop, threading.Thread]:
    loop = asyncio.new_event_loop()
    bus = SimpleBus()
    hub = IpcHub(HubSettings(port=0), tokens, registry, bus, runtime_dir)
    ready = threading.Event()

    def run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(hub.start())
        ready.set()
        loop.run_forever()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert ready.wait(5)
    return hub, bus, loop, t


def test_client_thread(
    tokens: TokenStore, registry: Any, runtime_dir: Path, hub_ref: dict[str, IpcHub]
) -> None:
    hub, bus, loop, thread = _run_hub_in_thread(tokens, registry, runtime_dir)
    hub_ref["hub"] = hub
    seen: list[str] = []
    dict_seen: list[dict[str, Any]] = []
    ct = IpcClientThread(
        hub.url, tokens.session_token, "shell", "shell:1", events=lambda e: seen.append(e.name)
    )
    ct.on_event(dict_seen.append)
    try:
        ct.start(timeout=5)
        assert ct.connected
        assert ct.call("state.get", {"path": "x"}).result(5)["path"] == "x"
        with pytest.raises(IpcError):
            ct.call("mode.set", {"mode": 1}).result(5)
        fut = asyncio.run_coroutine_threadsafe(
            bus.publish(Event(name="pet.state_changed", payload={"functional": "idle"})), loop
        )
        fut.result(5)
        for _ in range(50):
            if seen:
                break
            threading.Event().wait(0.05)
        assert seen == ["pet.state_changed"]
        assert (
            dict_seen[0]["name"] == "pet.state_changed"
            and dict_seen[0]["payload"]["functional"] == "idle"
        )
    finally:
        ct.stop()
        asyncio.run_coroutine_threadsafe(hub.stop(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
    assert not ct.connected
    with pytest.raises(IpcError):
        ct.call("state.get")


def test_client_thread_start_failure_surfaces() -> None:
    ct = IpcClientThread("ws://127.0.0.1:1/ws", "x" * 20, "shell", "shell:1", reconnect=False)
    with pytest.raises(IpcError):
        ct.start(timeout=5)
    ct.stop()


class _RestrictiveConsoleStderr:
    """Stands in for a real Windows console stream: `write()` raises `UnicodeEncodeError` for any
    non-ASCII text, exactly like a console fixed to a narrow codepage (cp1252/cp437/850) would for
    many extended characters - reproduced deterministically here regardless of the host OS/locale
    actually running the test."""

    encoding = "cp1252"

    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, s: str) -> int:
        if not s.isascii():
            raise UnicodeEncodeError(self.encoding, s, 0, 1, "character maps to <undefined>")
        self.writes.append(s)
        return len(s)

    def flush(self) -> None:
        pass


async def test_answer_survives_unicode_console_logging_failure(
    hub: IpcHub, tokens: TokenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the bug found by the Twitch agent: `_answer`'s generic-exception branch used
    to call `log.exception(...)` directly. On a Windows console forced to a narrow codepage, a
    handler exception whose message contains a non-encodable character (e.g. "ü") makes the
    logging call itself raise `UnicodeEncodeError`, which used to propagate out of `_answer` and
    skip sending the error reply entirely - the caller then hangs waiting for a response that never
    arrives. The fix logs defensively (string-only fields, wrapped so it can never raise) and
    always sends the reply."""
    console = _RestrictiveConsoleStderr()
    monkeypatch.setattr(sys, "stderr", console)
    configure_logging(None, level="ERROR", json_file=False, console=True)
    try:
        worker = IpcClient(
            hub.url, tokens.issue_worker_token("worker:crash"), "worker", "worker:crash"
        )

        async def boom(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
            raise ValueError("ü")

        worker.handle("boom.now", boom)
        await worker.connect()
        try:
            with pytest.raises(IpcError) as exc:
                await asyncio.wait_for(hub.request("worker:crash", "boom.now"), timeout=5)
            assert exc.value.code == ERR_INTERNAL
        finally:
            await worker.close()
    finally:
        shutdown_logging()

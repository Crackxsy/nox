"""IpcHub over real loopback WebSockets: auth, roles, subscriptions, streams, limits, workers."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from nox.core.bus import AsyncEventBus
from nox.core.config import IpcConfig
from nox.core.events import E, Event
from nox.ipc.client import IpcClient
from nox.ipc.dispatch import EmptyPayload, RequestRegistry
from nox.ipc.errors import (
    ERR_AUTH_DENIED,
    ERR_NOT_FOUND,
    ERR_PERMISSION,
    ERR_RATE_LIMITED,
    ERR_TIMEOUT,
    ERR_UNAVAILABLE,
    ERR_VALIDATION,
    IpcError,
)
from nox.ipc.protocol import (
    NAME_AUTH,
    NAME_ERROR,
    NAME_PING,
    NAME_PONG,
    NAME_SUBSCRIBE,
    Envelope,
    Kind,
    Source,
)
from nox.ipc.server import HubSettings, IpcHub, read_runtime_info, role_may_see
from nox.ipc.tokens import TokenStore

from .conftest import HubFactory, RawClient, SimpleBus, authed

RawFactory = Callable[..., Awaitable[RawClient]]

# ---- handshake ----------------------------------------------------------------------------------


async def test_no_auth_frame_closes_1008(hub_factory: HubFactory, raw: RawFactory) -> None:
    hub = await hub_factory(auth_timeout_s=0.3)
    c = await raw(url=hub.url)
    assert await c.wait_closed() == 1008


async def test_first_frame_not_auth_is_denied(raw: RawFactory) -> None:
    c = await raw()
    await c.send(Kind.REQUEST, NAME_PING)
    err = await c.recv()
    assert err.kind is Kind.ERROR and err.payload["code"] == ERR_AUTH_DENIED
    assert await c.wait_closed() == 1008


async def test_wrong_token_is_denied(raw: RawFactory) -> None:
    c = await raw()
    reply = await c.auth("x" * 40)
    assert reply.kind is Kind.ERROR
    assert reply.payload["code"] == ERR_AUTH_DENIED
    assert await c.wait_closed() == 1008


async def test_binary_or_garbage_first_frame_is_denied(raw: RawFactory) -> None:
    c = await raw()
    await c.send_raw(b"\x00\x01")
    err = await c.recv()
    assert err.payload["code"] == ERR_AUTH_DENIED
    assert await c.wait_closed() == 1008


async def test_auth_source_mismatch_is_denied(raw: RawFactory, tokens: TokenStore) -> None:
    c = await raw("pet", "pet:1")
    # envelope says pet, payload claims shell
    await c.send(
        Kind.REQUEST, NAME_AUTH, {"token": tokens.session_token, "role": "shell", "id": "shell:1"}
    )
    err = await c.recv()
    assert err.payload["code"] == ERR_AUTH_DENIED
    assert await c.wait_closed() == 1008


async def test_incompatible_client_major_is_denied(raw: RawFactory, tokens: TokenStore) -> None:
    c = await raw()
    reply = await c.auth(tokens.session_token, client_version="9.0.0")
    assert reply.payload["code"] == ERR_AUTH_DENIED
    c2 = await raw()
    reply2 = await c2.auth(tokens.session_token, client_version="0.1.0")
    assert reply2.kind is Kind.RESPONSE


async def test_successful_auth_and_ping(
    raw: RawFactory, tokens: TokenStore, hub: IpcHub, bus: SimpleBus
) -> None:
    c = await raw("shell", "shell:1")
    reply = await c.auth(tokens.session_token)
    assert reply.kind is Kind.RESPONSE and reply.name == NAME_AUTH
    assert reply.payload["ok"] is True
    assert reply.payload["schema_version"] == 1
    assert reply.payload["session_id"]
    pong = await c.request(NAME_PING, {"n": 1})
    assert pong.name == NAME_PONG and pong.payload["n"] == 1
    assert hub.find_client("shell:1") is not None
    assert E.IPC_CLIENT_CONNECTED in bus.names()
    await c.close()
    await asyncio.sleep(0.1)
    assert E.IPC_CLIENT_DISCONNECTED in bus.names()
    assert hub.find_client("shell:1") is None


async def test_second_auth_after_auth_is_rejected(raw: RawFactory, tokens: TokenStore) -> None:
    c = await authed(raw, tokens, "shell", "shell:1")
    reply = await c.auth(tokens.session_token)
    assert reply.kind is Kind.ERROR and reply.payload["code"] == ERR_VALIDATION


async def test_wrong_ws_path_is_404(hub: IpcHub) -> None:
    with pytest.raises(InvalidStatus) as exc:
        await connect(hub.url.replace("/ws", "/other"), open_timeout=5)
    assert exc.value.response.status_code == 404


# ---- requests -----------------------------------------------------------------------------------


async def test_request_round_trip(raw: RawFactory, tokens: TokenStore) -> None:
    c = await authed(raw, tokens, "dashboard", "dash:1")
    reply = await c.request("state.get", {"path": "pet"})
    assert reply.kind is Kind.RESPONSE
    assert reply.payload["path"] == "pet" and reply.payload["role"] == "dashboard"


async def test_unknown_request_not_found(raw: RawFactory, tokens: TokenStore) -> None:
    c = await authed(raw, tokens, "shell", "shell:1")
    reply = await c.request("nothing.here")
    assert reply.kind is Kind.ERROR and reply.name == NAME_ERROR
    assert reply.payload["code"] == ERR_NOT_FOUND


async def test_invalid_payload_validation_failed(raw: RawFactory, tokens: TokenStore) -> None:
    c = await authed(raw, tokens, "shell", "shell:1")
    reply = await c.request("mode.set", {"mode": 42})
    assert reply.payload["code"] == ERR_VALIDATION


async def test_role_enforcement(raw: RawFactory, tokens: TokenStore) -> None:
    pet = await authed(raw, tokens, "pet", "pet:1")
    assert (await pet.request("mode.set", {"mode": "coding"})).payload["code"] == ERR_PERMISSION
    assert (await pet.request("pet.interact", {"type": "click"})).kind is Kind.RESPONSE
    dash = await authed(raw, tokens, "dashboard", "dash:1")
    assert (await dash.request("security.permission.reply")).payload["code"] == ERR_PERMISSION
    assert (await dash.request("security.kill")).kind is Kind.RESPONSE
    assert (await dash.request("voice.ptt", {"pressed": True})).payload["code"] == ERR_PERMISSION
    shell = await authed(raw, tokens, "shell", "shell:1")
    assert (await shell.request("voice.ptt", {"pressed": True})).kind is Kind.RESPONSE
    assert (await shell.request("security.permission.reply")).kind is Kind.RESPONSE


async def test_source_spoofing_after_auth_is_rejected(raw: RawFactory, tokens: TokenStore) -> None:
    c = await authed(raw, tokens, "pet", "pet:1")
    spoof = Envelope(
        kind=Kind.REQUEST,
        name="mode.set",
        src=Source(role="shell", id="shell:1"),
        payload={"mode": "x"},
    )
    await c.send_raw(spoof.model_dump_json())
    err = await c.recv()
    assert err.kind is Kind.ERROR and err.corr == spoof.id
    assert err.payload["code"] == ERR_PERMISSION


async def test_malformed_frame_after_auth_is_error_not_close(
    raw: RawFactory, tokens: TokenStore
) -> None:
    c = await authed(raw, tokens, "shell", "shell:1")
    await c.send_raw("{not json")
    err = await c.recv()
    assert err.payload["code"] == ERR_VALIDATION
    await c.send_raw(json.dumps({"id": "abc", "kind": "request"}))
    err = await c.recv()
    assert err.payload["code"] == ERR_VALIDATION and err.corr == "abc"
    await c.send_raw(
        json.dumps(
            {
                "v": 2,
                "kind": "request",
                "name": "ipc.ping",
                "src": {"role": "shell", "id": "shell:1"},
            }
        )
    )
    err = await c.recv()
    assert "schema version" in err.payload["message"]
    assert (await c.request(NAME_PING)).name == NAME_PONG  # still connected


async def test_stream_frames_then_response(raw: RawFactory, tokens: TokenStore) -> None:
    c = await authed(raw, tokens, "dashboard", "dash:1")
    req = await c.send(Kind.REQUEST, "chat.send", {"text": "hello nox"})
    frames: list[Envelope] = []
    while True:
        env = await c.recv()
        assert env.corr == req.id
        frames.append(env)
        if env.kind is Kind.RESPONSE:
            break
    streams = [f for f in frames if f.kind is Kind.STREAM]
    assert [f.payload["delta"] for f in streams] == ["hello", "nox", ""]
    assert [f.payload["done"] for f in streams] == [False, False, True]
    assert all(f.name == "chat.send" for f in streams)
    assert frames[-1].payload["text"] == "HELLO NOX"


async def test_stream_frame_validated_against_registered_model(
    raw: RawFactory, tokens: TokenStore, registry: RequestRegistry
) -> None:
    """OP-9: a stream frame for a registered name (`chat.send` -> `ChatStreamFrame`) that does not
    match the model becomes `validation.failed`, not a silently malformed frame on the wire.
    """

    async def bad_chat_send(ctx: Any, p: Any) -> dict[str, Any]:
        await ctx.stream({"wrong_field": "oops"})  # missing required `delta`
        return {"request_id": "r1", "text": "x", "provider": "test", "degraded": False}

    registry.unregister("chat.send")
    registry.register("chat.send", EmptyPayload, bad_chat_send)
    c = await authed(raw, tokens, "dashboard", "dash:1")
    reply = await c.request("chat.send", {})
    assert reply.kind is Kind.ERROR
    assert reply.payload["code"] == ERR_VALIDATION


async def test_response_validated_against_registered_model(
    raw: RawFactory, tokens: TokenStore, registry: RequestRegistry
) -> None:
    """OP-9: a `chat.send` response that does not match `ChatSendResult` becomes `internal`,
    logged, and never reaches the client as a silently wrong-shaped response.
    """

    async def bad_chat_send(ctx: Any, p: Any) -> dict[str, Any]:
        return {"text": "missing other required fields"}

    registry.unregister("chat.send")
    registry.register("chat.send", EmptyPayload, bad_chat_send)
    c = await authed(raw, tokens, "dashboard", "dash:1")
    reply = await c.request("chat.send", {})
    assert reply.kind is Kind.ERROR
    assert reply.payload["code"] == "internal"


# ---- subscriptions and redaction ---------------------------------------------------------------


async def _publish(bus: SimpleBus, name: str, payload: dict[str, Any]) -> None:
    await bus.publish(Event(name=name, payload=payload))


async def test_default_subscription_is_system_only(
    raw: RawFactory, tokens: TokenStore, bus: SimpleBus
) -> None:
    c = await authed(raw, tokens, "shell", "shell:1")
    await _publish(bus, "pet.state_changed", {"functional": "idle"})
    await _publish(bus, "system.started", {})
    env = await c.recv()
    assert env.kind is Kind.EVENT and env.name == "system.started"
    assert env.src.role == "core"


async def test_subscribe_glob(raw: RawFactory, tokens: TokenStore, bus: SimpleBus) -> None:
    c = await authed(raw, tokens, "shell", "shell:1")
    reply = await c.request(NAME_SUBSCRIBE, {"patterns": ["pet.*", "privacy.**"]})
    assert reply.kind is Kind.RESPONSE and "pet.*" in reply.payload["patterns"]
    await _publish(bus, "voice.muted", {})
    await _publish(bus, "pet.state_changed", {"functional": "idle"})
    await _publish(
        bus,
        "privacy.capture_changed",
        {"microphone": False, "camera": False, "screen": False, "cloud": False},
    )
    names = [(await c.recv()).name for _ in range(2)]
    assert names == ["pet.state_changed", "privacy.capture_changed"]
    bad = await c.request(NAME_SUBSCRIBE, {"patterns": ["../etc"]})
    assert bad.payload["code"] == ERR_VALIDATION


@pytest.mark.parametrize(
    ("role", "name", "visible"),
    [
        ("pet", "voice.transcript_ready", False),
        ("pet", "voice.transcript_partial", False),
        ("pet", "ai.response_chunk", False),
        ("pet", "memory.created", False),
        ("pet", "pet.state_changed", True),
        ("remote", "memory.deleted", False),
        ("remote", "security.audit", False),
        ("dashboard", "voice.transcript_ready", True),
        ("dashboard", "security.audit", True),
        ("shell", "security.audit", True),
        ("worker", "security.audit", False),
        ("plugin", "security.audit", False),
        ("pet", "security.audit", False),
        ("pet", "twitch.chat_message", False),
        ("remote", "twitch.chat_message", False),
        ("dashboard", "twitch.chat_message", True),
        ("shell", "twitch.chat_message", True),
        ("pet", "twitch.chat_mood_changed", True),
    ],
)
def test_role_may_see(role: str, name: str, visible: bool) -> None:
    assert role_may_see(role, name) is visible


async def test_redaction_over_the_wire(raw: RawFactory, tokens: TokenStore, bus: SimpleBus) -> None:
    pet = await authed(raw, tokens, "pet", "pet:1")
    dash = await authed(raw, tokens, "dashboard", "dash:1")
    for c in (pet, dash):
        await c.request(NAME_SUBSCRIBE, {"patterns": ["**"]})
    transcript = {
        "text": "secret words",
        "language": "de",
        "confidence": 0.9,
        "addressed_to_nox": True,
        "duration_ms": 10,
        "latency_ms": 5,
    }
    await _publish(bus, "voice.transcript_ready", transcript)
    await _publish(
        bus,
        "security.audit",
        {
            "seq": 1,
            "actor": "a",
            "tool": "t",
            "action": "x",
            "decision": "allow",
            "result": "ok",
            "prev_hash": "0",
            "hash": "1",
        },
    )
    await _publish(bus, "pet.state_changed", {"functional": "idle"})
    dash_names = [(await dash.recv()).name for _ in range(3)]
    assert dash_names == ["voice.transcript_ready", "security.audit", "pet.state_changed"]
    pet_event = await pet.recv()
    assert pet_event.name == "pet.state_changed"
    with pytest.raises(TimeoutError):
        await pet.recv(timeout=0.2)


async def test_fan_out_with_real_async_event_bus(
    tokens: TokenStore, registry: Any, runtime_dir: Path
) -> None:
    bus = AsyncEventBus()
    hub = IpcHub(HubSettings(port=0), tokens, registry, bus, runtime_dir)
    await hub.start()
    try:
        client = IpcClient(hub.url, tokens.session_token, "shell", "shell:1")
        got: asyncio.Queue[Envelope] = asyncio.Queue()
        await client.subscribe(["pet.*"], lambda env: got.put_nowait(env))
        await client.connect()
        await bus.publish(Event(name="pet.state_changed", payload={"functional": "listening"}))
        env = await asyncio.wait_for(got.get(), 3)
        assert env.payload["functional"] == "listening"
        await client.close()
    finally:
        await hub.stop()


# ---- limits -------------------------------------------------------------------------------------


async def test_rate_limit_then_close(
    hub_factory: HubFactory, raw: RawFactory, tokens: TokenStore
) -> None:
    hub = await hub_factory(rate_per_s=1.0, rate_burst=5)
    c = await raw("shell", "shell:1", url=hub.url)
    assert (await c.auth(tokens.session_token)).kind is Kind.RESPONSE
    for _ in range(8):
        await c.send(Kind.REQUEST, NAME_PING)
    seen: list[Envelope] = []
    with pytest.raises(ConnectionClosed):
        while True:
            seen.append(await c.recv())
    codes = [e.payload.get("code") for e in seen if e.kind is Kind.ERROR]
    assert ERR_RATE_LIMITED in codes
    assert c.conn.close_code == 1008
    assert sum(1 for e in seen if e.name == NAME_PONG) <= 5


async def test_frame_over_1mib_closes_1009(raw: RawFactory, tokens: TokenStore) -> None:
    c = await authed(raw, tokens, "shell", "shell:1")
    big = Envelope(
        kind=Kind.REQUEST,
        name="state.get",
        src=c.source,
        payload={"path": "x" * (1024 * 1024 + 10)},
    )
    await c.send_raw(big.model_dump_json())
    assert await c.wait_closed() == 1009


async def test_inbound_events_only_from_workers_in_namespace(
    raw: RawFactory, tokens: TokenStore, bus: SimpleBus, hub: IpcHub
) -> None:
    shell = await authed(raw, tokens, "shell", "shell:1")
    ev = await shell.send(Kind.EVENT, "voice.muted", {})
    err = await shell.recv()
    assert err.payload["code"] == ERR_PERMISSION and err.corr == ev.id
    wt = tokens.issue_worker_token("worker:stt")
    w = await raw("worker", "worker:stt")
    assert (await w.auth(wt)).kind is Kind.RESPONSE
    ev = await w.send(Kind.EVENT, "stt.ready", {"model": "small"})
    assert (await w.recv()).payload["code"] == ERR_PERMISSION  # not declared yet
    assert (
        await w.request("worker.register", {"service": "stt", "services": ["stt"]})
    ).kind is Kind.RESPONSE
    await w.send(Kind.EVENT, "stt.ready", {"model": "small"})
    await asyncio.sleep(0.1)
    published = [e for e in bus.published if e.name == "stt.ready"]
    assert len(published) == 1 and published[0].source == "worker:stt"


# ---- workers -------------------------------------------------------------------------------------


async def test_worker_one_time_token(raw: RawFactory, tokens: TokenStore) -> None:
    wt = tokens.issue_worker_token("worker:tts")
    first = await raw("worker", "worker:tts")
    assert (await first.auth(wt)).kind is Kind.RESPONSE
    second = await raw("worker", "worker:tts")
    reply = await second.auth(wt)
    assert reply.kind is Kind.ERROR and reply.payload["code"] == ERR_AUTH_DENIED
    assert await second.wait_closed() == 1008
    shell = await raw("shell", "shell:1")
    assert (await shell.auth(tokens.issue_worker_token("worker:x"))).payload[
        "code"
    ] == ERR_AUTH_DENIED


async def test_hub_request_to_worker_with_stream(hub: IpcHub, tokens: TokenStore) -> None:
    worker = IpcClient(hub.url, tokens.issue_worker_token("worker:tts"), "worker", "worker:tts")

    async def speak(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
        await ctx.stream({"progress": 0.5})
        return {"done": True, "text": payload["text"]}

    async def fail(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
        raise IpcError(ERR_UNAVAILABLE, "engine down", retryable=True)

    worker.handle("tts.speak", speak)
    worker.handle("tts.fail", fail)
    await worker.connect()
    chunks: list[dict[str, Any]] = []
    result = await hub.request("worker:tts", "tts.speak", {"text": "hi"}, on_stream=chunks.append)
    assert result == {"done": True, "text": "hi"}
    assert chunks == [{"progress": 0.5, "done": False}]
    with pytest.raises(IpcError) as exc:
        await hub.request("worker:tts", "tts.fail")
    assert exc.value.code == ERR_UNAVAILABLE and exc.value.retryable
    with pytest.raises(IpcError) as exc:
        await hub.request("worker:tts", "tts.unknown")
    assert exc.value.code == ERR_NOT_FOUND
    with pytest.raises(IpcError) as exc:
        await hub.request("worker:nobody", "tts.speak")
    assert exc.value.code == ERR_UNAVAILABLE
    await worker.close()


async def test_hub_request_timeout(hub: IpcHub, tokens: TokenStore) -> None:
    worker = IpcClient(hub.url, tokens.issue_worker_token("worker:slow"), "worker", "worker:slow")

    async def slow(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
        await asyncio.sleep(2)
        return {}

    worker.handle("slow.op", slow)
    await worker.connect()
    with pytest.raises(IpcError) as exc:
        await hub.request("worker:slow", "slow.op", timeout=0.2)
    assert exc.value.code == ERR_TIMEOUT
    await worker.close()


async def test_hub_stream_helper(hub: IpcHub, tokens: TokenStore) -> None:
    client = IpcClient(hub.url, tokens.session_token, "dashboard", "dash:1")
    await client.connect()
    call = await client.request_stream("chat.send", {"text": "a b"})
    seen = [chunk async for chunk in call]
    assert [c["delta"] for c in seen] == ["a", "b", ""]
    assert (await call.result())["text"] == "A B"
    # hub.stream() by client id with explicit corr/name
    await hub.stream("dash:1", "corr-x", {"delta": "manual"}, True, name="chat.send")
    with pytest.raises(IpcError) as exc:
        await hub.stream("dash:1", "corr-y", {"not_delta": 1}, True, name="chat.send")
    assert exc.value.code == ERR_VALIDATION
    await client.close()


# ---- runtime info, ports, settings -------------------------------------------------------------


async def test_runtime_info_written_and_removed(hub_factory: HubFactory, runtime_dir: Path) -> None:
    hub = await hub_factory()
    info = read_runtime_info(runtime_dir)
    assert info["ws_port"] == hub.port and info["ws_url"] == hub.url
    assert info["schema_version"] == 1 and info["host"] == "127.0.0.1"
    await hub.stop()
    assert read_runtime_info(runtime_dir) == {}
    await hub.stop()  # idempotent


async def test_port_fallback(
    tokens: TokenStore, registry: Any, bus: SimpleBus, runtime_dir: Path
) -> None:
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy = blocker.getsockname()[1]
    hub = IpcHub(HubSettings(port=busy, port_attempts=6), tokens, registry, bus, runtime_dir)
    try:
        await hub.start()
        assert busy < hub.port <= busy + 5
    finally:
        await hub.stop()
        blocker.close()
    only = IpcHub(HubSettings(port=busy, port_attempts=1), tokens, registry, bus, runtime_dir)
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", busy))
    blocker.listen(1)
    try:
        with pytest.raises(OSError):
            await only.start()
    finally:
        blocker.close()


def test_settings_reject_non_loopback() -> None:
    with pytest.raises(ValidationError):
        HubSettings(host="0.0.0.0")  # noqa: S104
    with pytest.raises(ValidationError):
        HubSettings(host="192.168.1.5")
    assert (
        HubSettings.from_config(IpcConfig(host="127.0.0.1", port=47800, http_port=47801)).port
        == 47800
    )
    assert HubSettings(host="::1").host == "::1"

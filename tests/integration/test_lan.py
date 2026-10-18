"""Network access end to end: the real core with `ipc.lan.enabled`, real TLS, the real hub.

Every security control here has a test that goes red without it: the device role's exact request
surface, no session role over the network, no device role on loopback, the Origin check, a
forged bridge marker, revocation closing open sessions, and a device's "always" never becoming a
lasting grant.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from nox.app import DEFAULTS_PATH, PROFILES_DIR, NoxCore
from nox.core.config import load_config
from nox.ipc.dispatch import RequestContext
from nox.ipc.errors import ERR_PERMISSION, IpcError
from nox.ipc.protocol import NAME_AUTH, Envelope, Kind, Source
from nox.ipc.server import LAN_BRIDGE_HEADER
from nox.lan.install import LanRuntime
from tests._ports import free_port_base

#: What a paired device may call - the whole list. A request that appears here by accident (a new
#: handler registered with the wrong roles) or disappears (a surface cut off) turns this red.
DEVICE_SURFACE = frozenset(
    {
        "ai.providers",
        "capabilities.report",
        "chat.send",
        "clip.list",
        "config.effective",
        "config.get",
        "day.reminder.add",
        "day.reminder.cancel",
        "day.today",
        "extend.list",
        "health.get",
        "health.history",
        "home.command",
        "home.light",
        "home.list",
        "home.scene",
        "home.status",
        "home.switch",
        "home.test",
        "lan.devices.list",
        "lan.pair.start",
        "lan.status",
        "lan.unpair",
        "memory.profile.forget",
        "memory.profile.list",
        "mode.set",
        "personality.get",
        "plans.approve",
        "plans.discard",
        "plans.list",
        "plugin.status",
        "presets.activate",
        "presets.list",
        "secrets.status",
        "security.kill",
        "security.panic",
        "security.permission.reply",
        "security.pin.status",
        "state.get",
        "stream.funken.top",
        "stream.session.status",
        "twitch.auth.status",
        "views.clear",
        "views.list",
        "voice.mute",
    }
)

#: Administration that must stay on the machine Nox runs on. Each one is its own negative test.
ADMINISTRATION = (
    "config.set",
    "secrets.set",
    "secrets.delete",
    "privacy.set",
    "security.resume",
    "personality.set",
    "twitch.auth.start",
    "twitch.auth.disconnect",
    "memory.profile.add",
    "presets.test_action",
    "clip.tag",
    "clip.export",
    "clip.trim",
    "remote.pair.start",
    "remote.unpair",
    "remote.devices.list",
    "voice.ptt",
    "pet.interact",
    "worker.register",
)


def _config(tmp_path: Path) -> Any:
    base = free_port_base()
    settings = {
        "paths": {
            name: str(tmp_path / name.removesuffix("_dir"))
            for name in (
                "data_dir",
                "vault_dir",
                "index_dir",
                "database_dir",
                "cache_dir",
                "backups_dir",
                "runtime_dir",
                "logs_dir",
            )
        },
        "ipc": {
            "port": base,
            "http_port": base + 1,
            "lan": {"enabled": True, "host": "127.0.0.1", "port": 0},
        },
        "health": {"check_interval_s": 3600},
        "logging": {"level": "WARNING"},
    }
    (tmp_path / "vault").mkdir()
    return load_config(DEFAULTS_PATH, None, None, settings)


@pytest.fixture
async def core(tmp_path: Path) -> AsyncIterator[NoxCore]:
    core = NoxCore(_config(tmp_path), voice=False, profiles_dir=PROFILES_DIR)
    await core.start()
    try:
        runtime = _lan(core)
        for _ in range(200):
            if runtime.running or runtime.error:
                break
            await asyncio.sleep(0.05)
        assert runtime.running, runtime.error
        yield core
    finally:
        await core.stop()


def _lan(core: NoxCore) -> LanRuntime:
    runtime = core.extensions["lan"]
    assert isinstance(runtime, LanRuntime)
    return runtime


def _base(core: NoxCore) -> str:
    return f"https://127.0.0.1:{_lan(core).port}"


def _ssl(core: NoxCore) -> ssl.SSLContext:
    tls = _lan(core).tls
    assert tls is not None
    return ssl.create_default_context(cafile=str(tls.ca_file))


async def _pair(core: NoxCore, name: str = "Testgerät") -> dict[str, str]:
    start = await _lan(core).devices.start()
    tls = _lan(core).tls
    assert tls is not None
    async with httpx.AsyncClient(verify=str(tls.ca_file)) as client:
        response = await client.post(
            f"{_base(core)}/api/pair",
            json={"code": start.code, "name": name},
            headers={"Origin": _base(core)},
        )
    assert response.status_code == 201, response.text
    body: dict[str, str] = response.json()
    return body


async def _connect(core: NoxCore, *, origin: str | None = None) -> ClientConnection:
    return await connect(
        f"wss://127.0.0.1:{_lan(core).port}/ws",
        ssl=_ssl(core),
        origin=origin or _base(core),  # type: ignore[arg-type]
        open_timeout=5,
    )


async def _auth(ws: ClientConnection, *, token: str, role: str, client_id: str) -> Envelope:
    src = Source(role=role, id=client_id)  # type: ignore[arg-type]
    env = Envelope(
        kind=Kind.REQUEST,
        name=NAME_AUTH,
        src=src,
        payload={"token": token, "role": role, "id": client_id},
    )
    await ws.send(env.model_dump_json())
    return Envelope.model_validate(json.loads(await ws.recv()))


async def _request(
    ws: ClientConnection, client_id: str, name: str, payload: dict[str, Any]
) -> Envelope:
    env = Envelope(
        kind=Kind.REQUEST, name=name, src=Source(role="device", id=client_id), payload=payload
    )
    await ws.send(env.model_dump_json())
    while True:
        reply = Envelope.model_validate(json.loads(await asyncio.wait_for(ws.recv(), 5)))
        if reply.corr == env.id:
            return reply


async def _local(core: NoxCore, role: str, name: str, payload: dict[str, Any]) -> dict[str, Any]:
    assert core.registry is not None
    env = Envelope(
        kind=Kind.REQUEST, name=name, src=Source(role=role, id=f"{role}:t"), payload=payload
    )  # type: ignore[arg-type]
    reply = await core.registry.dispatch(
        RequestContext(client_id=f"{role}:t", role=role, request=env), env
    )
    if reply.kind is Kind.ERROR:
        raise IpcError.from_payload(reply.payload)
    return reply.payload


# ---- what a device may do ------------------------------------------------------------------------


async def test_the_device_surface_is_exactly_the_list(core: NoxCore) -> None:
    assert core.registry is not None
    allowed = {n for n in core.registry.names() if core.registry.is_allowed(n, "device")}
    assert allowed == DEVICE_SURFACE


@pytest.mark.parametrize("name", ADMINISTRATION)
async def test_administration_is_refused_to_a_device(core: NoxCore, name: str) -> None:
    assert core.registry is not None
    if core.registry.get(name) is None:
        pytest.skip(f"{name} is not registered in this build")
    assert not core.registry.is_allowed(name, "device")
    assert core.registry.is_allowed(name, "dashboard") or name in {
        "voice.ptt",
        "pet.interact",
        "worker.register",
    }


# ---- end to end over TLS -------------------------------------------------------------------------


async def test_a_paired_device_talks_to_nox_over_the_network(core: NoxCore) -> None:
    paired = await _pair(core)
    async with await _connect(core) as ws:
        auth = await _auth(ws, token=paired["token"], role="device", client_id=paired["client_id"])
        assert auth.kind is Kind.RESPONSE and auth.payload["ok"] is True
        listing = await _request(ws, paired["client_id"], "lan.devices.list", {})
        assert listing.kind is Kind.RESPONSE
        mine = [d for d in listing.payload["devices"] if d["current"]]
        assert [d["id"] for d in mine] == [paired["device_id"]]
        refused = await _request(ws, paired["client_id"], "config.set", {"values": {}})
        assert refused.kind is Kind.ERROR and refused.payload["code"] == ERR_PERMISSION


async def test_the_session_token_is_useless_over_the_network(core: NoxCore) -> None:
    assert core.tokens is not None
    async with await _connect(core) as ws:
        auth = await _auth(
            ws, token=core.tokens.session_token, role="dashboard", client_id="dashboard:x"
        )
        assert auth.kind is Kind.ERROR


async def test_a_device_token_is_useless_on_loopback(core: NoxCore) -> None:
    paired = await _pair(core)
    assert core.hub is not None
    async with connect(f"ws://127.0.0.1:{core.hub.port}/ws", open_timeout=5) as ws:
        auth = await _auth(ws, token=paired["token"], role="device", client_id=paired["client_id"])
        assert auth.kind is Kind.ERROR


async def test_a_forged_bridge_marker_is_refused(core: NoxCore) -> None:
    assert core.hub is not None
    with pytest.raises(InvalidStatus) as refused:
        await connect(
            f"ws://127.0.0.1:{core.hub.port}/ws",
            additional_headers={LAN_BRIDGE_HEADER: "guessed"},
            open_timeout=5,
        )
    assert refused.value.response.status_code == 403


async def test_another_origin_cannot_connect(core: NoxCore) -> None:
    with pytest.raises((InvalidStatus, ConnectionClosed)):
        async with await _connect(core, origin="https://evil.example") as ws:
            await ws.recv()


async def test_revoking_closes_the_open_session_at_once(core: NoxCore) -> None:
    paired = await _pair(core)
    async with await _connect(core) as ws:
        await _auth(ws, token=paired["token"], role="device", client_id=paired["client_id"])
        result = await _local(core, "dashboard", "lan.unpair", {"device_id": paired["device_id"]})
        assert result["ok"] and result["closed_sessions"] == 1
        with pytest.raises(ConnectionClosed) as closed:
            while True:
                await asyncio.wait_for(ws.recv(), 5)
        assert closed.value.rcvd is not None and closed.value.rcvd.code == 1008
    async with await _connect(core) as ws:
        again = await _auth(ws, token=paired["token"], role="device", client_id=paired["client_id"])
        assert again.kind is Kind.ERROR


async def test_a_device_cannot_pair_or_remove_others_without_a_pin(core: NoxCore) -> None:
    paired = await _pair(core)
    other = await _pair(core, "Zweites Gerät")
    async with await _connect(core) as ws:
        await _auth(ws, token=paired["token"], role="device", client_id=paired["client_id"])
        pair = await _request(ws, paired["client_id"], "lan.pair.start", {})
        assert pair.kind is Kind.ERROR and pair.payload["code"] == ERR_PERMISSION
        remove = await _request(
            ws, paired["client_id"], "lan.unpair", {"device_id": other["device_id"]}
        )
        assert remove.kind is Kind.ERROR and remove.payload["code"] == ERR_PERMISSION
        # Signing itself out needs nothing.
        own = await _request(
            ws, paired["client_id"], "lan.unpair", {"device_id": paired["device_id"]}
        )
        assert own.kind is Kind.RESPONSE and own.payload["ok"] is True


async def test_the_local_dashboard_pairs_with_a_link_and_qr(core: NoxCore) -> None:
    result = await _local(core, "dashboard", "lan.pair.start", {"name": "Küche"})
    assert result["url"].startswith("https://") and f"#pair={result['code']}" in result["url"]
    assert result["qr"].startswith("data:image/svg+xml")
    assert result["fingerprint"] == _lan(core).tls.fingerprint  # type: ignore[union-attr]


async def test_a_devices_always_is_not_remembered(core: NoxCore) -> None:
    assert core.security is not None
    engine = core.security.engine
    result = await _local(
        core,
        "device",
        "security.permission.reply",
        {"grant_id": "unknown", "decision": "allow", "remember": True},
    )
    assert result == {"ok": False, "remembered": False}
    seen: list[tuple[Any, ...]] = []
    original = engine.reply

    def spy(grant_id: str, decision: Any, *, remember: bool = False, by: str = "user") -> bool:
        seen.append((grant_id, remember, by))
        return original(grant_id, decision, remember=remember, by=by)

    engine.reply = spy  # type: ignore[method-assign]
    await _local(
        core,
        "device",
        "security.permission.reply",
        {"grant_id": "g", "decision": "allow", "remember": True},
    )
    await _local(
        core,
        "dashboard",
        "security.permission.reply",
        {"grant_id": "g", "decision": "allow", "remember": True},
    )
    assert seen == [("g", False, "device:t"), ("g", True, "dashboard:t")]


async def test_nox_pair_prints_a_link_a_code_and_the_fingerprint(core: NoxCore) -> None:
    from nox.lan.terminal import pair_in_terminal

    printed: list[str] = []
    await asyncio.to_thread(
        pair_in_terminal,
        Path(core.config.paths.runtime_dir),
        name="Tablet",
        language="en",
        ask_pin=lambda _prompt: pytest.fail("no PIN is set, none may be asked for"),
        say=printed.append,
    )
    (text,) = printed
    assert "#pair=" in text and "https://" in text
    assert _lan(core).tls.fingerprint[:23] in text  # type: ignore[union-attr]
    assert "Valid until" in text

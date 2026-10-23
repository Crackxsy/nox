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
        "memory.profile.list",
        "node.computer.status",  # the header line about a PC Nox controls (read only)
        "node.defaults.set",  # with the PIN, always (`nox.node.policy`)
        "node.link",
        "node.list",
        "node.narrow",  # with the PIN from a device, like node.unpair
        "node.pair.start",
        "node.unpair",
        # Nox-OS remote maintenance: switching on needs the PIN, always (`nox.noxos`); a headless
        # Pi is administered from a paired device.
        "noxos.ssh.disable",
        "noxos.ssh.enable",
        "noxos.ssh.keys.set",
        "noxos.ssh.passphrase.change",
        "noxos.ssh.passphrase.set",
        "noxos.ssh.status",
        "personality.get",
        "plans.approve",
        "plans.discard",
        "plans.list",
        "plugin.status",
        "presets.list",
        "secrets.status",
        "security.kill",
        "security.panic",
        "security.permission.pending",
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
    "node.computer.release",  # only on the computer Nox runs on, with the PIN
    "secrets.set",
    "secrets.delete",
    "privacy.set",
    "security.resume",
    "personality.set",
    "twitch.auth.start",
    "twitch.auth.disconnect",
    "memory.profile.add",
    "memory.profile.confirm",
    "memory.profile.forget",
    "consent.list",
    "consent.grant",
    "consent.revoke",
    "skills.list",
    "skills.activate",
    "skills.deactivate",
    "skills.forget",
    "mcp.servers.list",
    "mcp.servers.add",
    "mcp.servers.approve",
    "mcp.servers.enable",
    "mcp.servers.remove",
    "watch.list",
    "watch.delete",
    "ai.setup.status",
    "ai.setup.check",
    "ai.setup.start",
    "voice.model.status",
    "voice.model.download",
    "mode.set",
    "presets.activate",
    "presets.test_action",
    "clip.tag",
    "clip.export",
    "clip.trim",
    "stream.overlay.status",
    "stream.overlay.setup",
    "stream.overlay.revoke",
    "stream.audio_track.setup",
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
        # On, so its requests exist and their refusal to a device is actually checked.
        "remote": {"enabled": True},
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


async def _pair(core: NoxCore, name: str = "Test device") -> dict[str, str]:
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
    # A renamed request must turn this red, not quietly skip its refusal.
    assert core.registry.get(name) is not None, f"{name} is not registered"
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


@pytest.mark.parametrize("origin", ["https://evil.example", None], ids=["foreign", "none"])
async def test_another_origin_cannot_even_open_a_websocket(
    core: NoxCore, origin: str | None
) -> None:
    """Refused at the upgrade with 403 - not accepted and closed later by the hub's auth timeout,
    which a test accepting any close would not tell apart."""
    with pytest.raises(InvalidStatus) as refused:
        await connect(
            f"wss://127.0.0.1:{_lan(core).port}/ws",
            ssl=_ssl(core),
            origin=origin,  # type: ignore[arg-type]
            open_timeout=5,
        )
    assert refused.value.response.status_code == 403


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
    other = await _pair(core, "Second device")
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
    result = await _local(core, "dashboard", "lan.pair.start", {"name": "Kitchen"})
    assert result["url"].startswith("https://") and f"#pair={result['code']}" in result["url"]
    assert result["qr"].startswith("data:image/svg+xml")
    assert result["fingerprint"] == _lan(core).tls.server_fingerprint  # type: ignore[union-attr]
    assert result["ca_fingerprint"] == _lan(core).tls.fingerprint  # type: ignore[union-attr]


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
    assert _lan(core).tls.server_fingerprint[:23] in text  # type: ignore[union-attr]
    assert "Valid until" in text


async def test_nox_devices_lists_the_paired_devices(core: NoxCore) -> None:
    from nox.lan.terminal import devices_in_terminal

    runtime_dir = Path(core.config.paths.runtime_dir)
    printed: list[str] = []
    await asyncio.to_thread(devices_in_terminal, runtime_dir, language="en", say=printed.append)
    assert printed == ["No device paired yet. Pair one: nox pair"]

    kitchen = await _pair(core, "Kitchen tablet")
    await _pair(core, "Sam's phone")
    await _local(core, "dashboard", "lan.unpair", {"device_id": kitchen["device_id"]})
    printed.clear()
    await asyncio.to_thread(devices_in_terminal, runtime_dir, language="en", say=printed.append)
    (text,) = printed
    lines = text.splitlines()
    assert lines[0] == "Paired devices:"
    # Active devices first, removed ones after them.
    assert "Sam's phone" in lines[1] and "never" in lines[1]
    assert "Kitchen tablet" in lines[2] and "removed" in lines[2]
    assert kitchen["device_id"][:8] in lines[2]


async def test_nox_unpair_removes_a_lost_phone_by_name(core: NoxCore) -> None:
    from nox.lan.terminal import unpair_in_terminal

    _with_pin(core, "4711")  # the computer itself needs no PIN to narrow access
    paired = await _pair(core, "Sam's phone")
    ws = await _connect(core)
    client_id = f"device:{paired['device_id']}"
    signed_in = await _auth(ws, token=paired["token"], role="device", client_id=client_id)
    assert signed_in.payload["ok"]
    printed: list[str] = []
    await asyncio.to_thread(
        unpair_in_terminal,
        Path(core.config.paths.runtime_dir),
        "SAM'S PHONE",
        language="en",
        say=printed.append,
    )
    assert printed == ["Sam's phone is removed. Its open connections have ended."]
    with pytest.raises(ConnectionClosed):
        while True:
            await asyncio.wait_for(ws.recv(), 5)
    assert _lan(core).devices.verify(paired["token"]) is None


async def test_nox_unpair_refuses_an_unknown_or_ambiguous_name(core: NoxCore) -> None:
    from nox.lan.terminal import PairingError, unpair_in_terminal

    runtime_dir = Path(core.config.paths.runtime_dir)
    first = await _pair(core, "Tablet")
    second = await _pair(core, "Tablet")
    with pytest.raises(PairingError, match="No paired device"):
        await asyncio.to_thread(unpair_in_terminal, runtime_dir, "Fridge", language="en")
    with pytest.raises(PairingError, match="Several devices") as refused:
        await asyncio.to_thread(unpair_in_terminal, runtime_dir, "tablet", language="en")
    assert first["device_id"][:8] in str(refused.value)
    # Nothing was removed on a refusal; the id then picks exactly one.
    assert _lan(core).devices.verify(first["token"]) is not None
    await asyncio.to_thread(
        unpair_in_terminal, runtime_dir, second["device_id"][:8], language="en", say=print
    )
    assert _lan(core).devices.verify(second["token"]) is None
    assert _lan(core).devices.verify(first["token"]) is not None


async def test_with_a_pin_a_device_pairs_only_with_the_right_one(core: NoxCore) -> None:
    from nox.security.gate import SecurityChangeGate
    from nox.security.secrets import InMemorySecretStore, PinManager

    assert core.security is not None
    # Never the real credential manager in a test.
    core.security.secrets = InMemorySecretStore()
    core.security.pin = PinManager(
        core.security.secrets, audit=core.security.audit, prefer_argon2=False
    )
    core.security.gate = SecurityChangeGate(
        core.security.pin, required=True, audit=core.security.audit
    )
    core.security.pin.set_pin("4711", by="test")

    paired = await _pair(core)
    async with await _connect(core) as ws:
        await _auth(ws, token=paired["token"], role="device", client_id=paired["client_id"])
        without = await _request(ws, paired["client_id"], "lan.pair.start", {})
        assert without.kind is Kind.ERROR and without.payload["code"] == ERR_PERMISSION
        wrong = await _request(ws, paired["client_id"], "lan.pair.start", {"pin": "0000"})
        assert wrong.kind is Kind.ERROR and wrong.payload["code"] == ERR_PERMISSION
        right = await _request(ws, paired["client_id"], "lan.pair.start", {"pin": "4711"})
        assert right.kind is Kind.RESPONSE and len(right.payload["code"]) == 8


# ---- what a paired device may allow, and what removes it ----------------------------------------


def _with_pin(core: NoxCore, pin: str | None) -> None:
    """A PIN on an in-memory secret store - never the machine's real credential manager."""
    from nox.security.gate import SecurityChangeGate
    from nox.security.secrets import InMemorySecretStore, PinManager

    assert core.security is not None
    core.security.secrets = InMemorySecretStore()
    core.security.pin = PinManager(
        core.security.secrets, audit=core.security.audit, prefer_argon2=False
    )
    core.security.gate = SecurityChangeGate(
        core.security.pin, required=True, audit=core.security.audit
    )
    if pin:
        core.security.pin.set_pin(pin, by="test")


def _ask(core: NoxCore, risk: Any) -> str:
    from nox.security.model import PermissionRequest

    assert core.security is not None
    return core.security.engine.request_confirmation(
        PermissionRequest(
            agent="companion", tool="file", action="delete", mode="companion", risk=risk
        )
    )


async def test_a_device_needs_the_pin_to_allow_something_risky(core: NoxCore) -> None:
    """Otherwise a stolen phone asks Nox for something in the chat and allows it itself."""
    from nox.security.model import Risk

    _with_pin(core, None)
    paired = await _pair(core)
    async with await _connect(core) as ws:
        await _auth(ws, token=paired["token"], role="device", client_id=paired["client_id"])
        grant = _ask(core, Risk.HIGH)
        allow = {"grant_id": grant, "decision": "allow", "remember": False}
        no_pin_set = await _request(ws, paired["client_id"], "security.permission.reply", allow)
        assert no_pin_set.kind is Kind.ERROR
        assert no_pin_set.payload["details"]["reason"] == "pin_not_set"
        # Saying no never needs anything.
        deny = {"grant_id": grant, "decision": "deny", "remember": False}
        denied = await _request(ws, paired["client_id"], "security.permission.reply", deny)
        assert denied.kind is Kind.RESPONSE and denied.payload["ok"] is True

        _with_pin(core, "4711")
        grant = _ask(core, Risk.HIGH)
        allow["grant_id"] = grant
        without = await _request(ws, paired["client_id"], "security.permission.reply", allow)
        assert without.payload["details"]["reason"] == "pin_required"
        right = await _request(
            ws, paired["client_id"], "security.permission.reply", {**allow, "pin": "4711"}
        )
        assert right.kind is Kind.RESPONSE and right.payload["ok"] is True

        # Low risk: a tap is enough, as on the PC.
        grant = _ask(core, Risk.LOW)
        low = await _request(
            ws,
            paired["client_id"],
            "security.permission.reply",
            {"grant_id": grant, "decision": "allow", "remember": False},
        )
        assert low.kind is Kind.RESPONSE and low.payload["ok"] is True


async def test_a_device_needs_the_pin_to_approve_a_plan(core: NoxCore) -> None:
    _with_pin(core, None)
    paired = await _pair(core)
    async with await _connect(core) as ws:
        await _auth(ws, token=paired["token"], role="device", client_id=paired["client_id"])
        refused = await _request(ws, paired["client_id"], "plans.approve", {"plan": "p1"})
        assert refused.kind is Kind.ERROR and refused.payload["code"] == ERR_PERMISSION


async def test_the_pc_removes_a_lost_phone_without_the_pin(core: NoxCore) -> None:
    """A thief must not be able to block the removal by locking the PIN."""
    _with_pin(core, "4711")
    paired = await _pair(core)
    removed = await _local(core, "dashboard", "lan.unpair", {"device_id": paired["device_id"]})
    assert removed["ok"] is True


async def test_three_wrong_pins_remove_the_device(core: NoxCore) -> None:
    _with_pin(core, "4711")
    paired = await _pair(core)
    async with await _connect(core) as ws:
        await _auth(ws, token=paired["token"], role="device", client_id=paired["client_id"])
        reasons = []
        for _ in range(3):
            answer = await _request(ws, paired["client_id"], "lan.pair.start", {"pin": "0000"})
            reasons.append(answer.payload["details"]["reason"])
        assert reasons == ["pin_wrong", "pin_wrong", "removed"]
        with pytest.raises(ConnectionClosed) as closed:
            while True:
                await asyncio.wait_for(ws.recv(), 5)
        assert closed.value.rcvd is not None and closed.value.rcvd.code == 1008
    assert all(d.revoked for d in _lan(core).devices.devices() if d.id == paired["device_id"])
    # The PIN itself is not locked: the owner still uses it.
    assert core.security is not None
    status = await core.security.pin.verify_pin_async("4711", by="test")
    assert status.ok


async def test_signing_out_closes_the_session_as_signed_out(core: NoxCore) -> None:
    paired = await _pair(core)
    async with await _connect(core) as ws:
        await _auth(ws, token=paired["token"], role="device", client_id=paired["client_id"])
        answer = await _request(
            ws, paired["client_id"], "lan.unpair", {"device_id": paired["device_id"]}
        )
        assert answer.kind is Kind.RESPONSE and answer.payload["ok"] is True
        with pytest.raises(ConnectionClosed) as closed:
            while True:
                await asyncio.wait_for(ws.recv(), 5)
        assert closed.value.rcvd is not None and closed.value.rcvd.code == 4001


async def test_open_questions_reach_a_dashboard_that_connects_later(core: NoxCore) -> None:
    from nox.security.model import Risk

    grant = _ask(core, Risk.MEDIUM)
    paired = await _pair(core)
    async with await _connect(core) as ws:
        await _auth(ws, token=paired["token"], role="device", client_id=paired["client_id"])
        listed = await _request(ws, paired["client_id"], "security.permission.pending", {})
        assert listed.kind is Kind.RESPONSE
        (pending,) = listed.payload["pending"]
        assert pending["request_id"] == grant and pending["risk"] == "medium"
        assert pending["expires_at"] and pending["remember_ttl_s"] == 8 * 3600


async def test_a_device_sees_whether_a_pin_is_set_but_no_local_detail(core: NoxCore) -> None:
    paired = await _pair(core)
    _lan(core).error = "certificates: OSError: C:/somewhere/private"
    async with await _connect(core) as ws:
        await _auth(ws, token=paired["token"], role="device", client_id=paired["client_id"])
        status = await _request(ws, paired["client_id"], "lan.status", {})
        assert status.payload["pin_set"] is False
        assert status.payload["error"] == "unavailable"
    local = await _local(core, "dashboard", "lan.status", {})
    assert local["error"].startswith("certificates:")


#: Every registered request the calm rule lets through while Nox controls mouse and keyboard
#: (`nox.ipc.dispatch.calm_ui_request`: the explicit list, `ipc.*`, `*.list`, `*.status`). A new
#: name here must only read or stop: a state-changing `x.status` turns this red on purpose.
CALM_SNAPSHOT = frozenset(
    {
        "ai.setup.status",
        "clip.list",
        "config.effective",
        "config.get",
        "consent.list",
        "day.today",
        "extend.list",
        "health.get",
        "health.history",
        "home.list",
        "home.status",
        "lan.devices.list",
        "lan.status",
        "mcp.servers.list",
        "mcp.tools.list",
        "memory.profile.list",
        "node.computer.status",
        "node.list",
        "personality.get",
        "plans.list",
        "plugin.status",
        "presets.list",
        "remote.devices.list",
        "secrets.status",
        "security.kill",
        "security.panic",
        "security.permission.pending",
        "security.permission.reply",
        "security.pin.status",
        "skills.list",
        "state.get",
        "stream.overlay.status",
        "stream.session.status",
        "twitch.auth.status",
        "views.list",
        "voice.model.status",
        "voice.mute",
        "watch.list",
    }
)


async def test_the_calm_requests_are_exactly_the_snapshot(core: NoxCore) -> None:
    from nox.ipc.dispatch import calm_ui_request

    assert core.registry is not None
    calm = {n for n in core.registry.names() if calm_ui_request(n)}
    assert calm == CALM_SNAPSHOT


async def test_a_paired_devices_request_carries_its_network_address(core: NoxCore) -> None:
    """P1: a request bridged in by the network listener knows the device's address
    (`RequestContext.peer`); a client on this machine has none."""
    from pydantic import BaseModel

    assert core.registry is not None and core.hub is not None and core.tokens is not None
    seen: list[tuple[str, str]] = []

    class Empty(BaseModel):
        pass

    async def handler(ctx: Any, _p: Empty) -> dict[str, Any]:
        seen.append((ctx.role, ctx.peer))
        return {}

    core.registry.register("zz.peer", Empty, handler, roles=("dashboard", "device"))
    paired = await _pair(core)
    async with await _connect(core) as ws:
        await _auth(ws, token=paired["token"], role="device", client_id=paired["client_id"])
        assert (await _request(ws, paired["client_id"], "zz.peer", {})).kind is Kind.RESPONSE
    async with connect(f"ws://127.0.0.1:{core.hub.port}/ws", open_timeout=5) as local:
        await _auth(
            local, token=core.tokens.session_token, role="dashboard", client_id="dashboard:p"
        )
        env = Envelope(
            kind=Kind.REQUEST,
            name="zz.peer",
            src=Source(role="dashboard", id="dashboard:p"),
            payload={},
        )
        await local.send(env.model_dump_json())
        while True:
            reply = Envelope.model_validate(json.loads(await asyncio.wait_for(local.recv(), 5)))
            if reply.corr == env.id:
                break
        assert reply.kind is Kind.RESPONSE, reply.payload
    assert seen == [("device", "127.0.0.1"), ("dashboard", "")]

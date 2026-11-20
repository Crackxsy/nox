"""The pet overlay on a node: a loopback-only relay that swaps in the node's read-only,
rotating credential, and the core revoking that credential when OBS is narrowed or the
node is removed."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from nox.ipc.overlay_tokens import OverlayTokenStore
from nox.ipc.tokens import TokenStore
from nox.lan.tls import TlsMaterial
from nox.node.device.overlay import PLACEHOLDER, OverlayRelay, local_origin, overlay_url, swap_token
from nox.node.hub.pairing import NodePairing
from nox.node.hub.repo import NodeRepository
from nox.node.hub.satellites import node_overlay_label
from nox.node.wire import protocol as p
from tests.unit.node.support_voice import PC, PI, fake_session, lan_accepts, voice_hub

pytestmark = pytest.mark.usefixtures("a_screen")


WS_URL = "ws://127.0.0.1:47845/ws"


# ---- the overlay relay --------------------------------------------------------------------------


def _auth(role: str = "overlay", token: str = PLACEHOLDER) -> str:
    return json.dumps(
        {
            "v": 1,
            "id": "1",
            "kind": "request",
            "name": "ipc.auth",
            "src": {"role": role, "id": f"{role}:x"},
            "payload": {"token": token, "role": role, "id": f"{role}:x"},
        }
    )


def test_the_relay_swaps_in_its_credential_for_an_overlay_only() -> None:
    swapped = swap_token(_auth(), "t" * 43)
    assert swapped is not None and json.loads(swapped)["payload"]["token"] == "t" * 43
    assert swap_token(_auth("device"), "t" * 43) is None
    assert swap_token(_auth("dashboard"), "t" * 43) is None
    assert swap_token("not json", "t" * 43) is None
    assert local_origin(None, 47845) and local_origin("http://127.0.0.1:47845", 47845)
    assert not local_origin("https://example.invalid", 47845)
    url = overlay_url(47845, PI)
    assert url.endswith(f"&device={PI}#overlay={PLACEHOLDER}") and "127.0.0.1" in url


def _relay(
    tmp_path: Path,
    *,
    allowed: bool = True,
    token: str | None = "n" * 43,
    ca_file: Path | None = None,
) -> tuple[OverlayRelay, dict[str, Any]]:
    from nox.node.device.home import NodeIdentity

    seen: dict[str, Any] = {"upstream": [], "credentials": 0, "fetched": []}
    identity = NodeIdentity(
        node_id=PI,
        name="Living room Pi",
        core_host="nox-core.example.invalid",
        core_port=47843,
        node_port=47844,
        ca_fingerprint="f" * 64,
        paired_at=datetime(2026, 10, 22, tzinfo=UTC),
    )

    async def credential() -> str | None:
        seen["credentials"] += 1
        return token

    class Upstream:
        close_code = 1000

        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, text: str) -> None:
            self.sent.append(text)

        def __aiter__(self) -> Upstream:
            self._done = False
            return self

        async def __anext__(self) -> str:
            if self._done:
                await asyncio.sleep(3600)
            self._done = True
            return json.dumps({"kind": "response", "name": "ipc.auth", "payload": {"ok": True}})

        async def close(self) -> None:
            return None

    async def connect(url: str, **kw: Any) -> Upstream:
        upstream = Upstream()
        seen["upstream"].append((url, kw, upstream))
        return upstream

    async def fetch(path: str) -> tuple[int, str, bytes]:
        seen["fetched"].append(path)
        return 200, "text/html", b"<html>pet</html>"

    relay = OverlayRelay(
        identity=lambda: identity,
        ca_file=ca_file or tmp_path / "ca.pem",
        allowed=lambda: allowed,
        credential=credential,
        fetch=fetch,
        connect=connect,
    )
    return relay, seen


def test_the_relay_serves_the_page_on_loopback_only(tmp_path: Path) -> None:
    from starlette.testclient import TestClient

    relay, seen = _relay(tmp_path)
    client = TestClient(relay.app(), base_url="http://127.0.0.1:47845")
    page = client.get("/pet/index.html")
    assert page.status_code == 200 and seen["fetched"] == ["/pet/index.html"]
    assert "ws://127.0.0.1:47845" in page.headers["content-security-policy"]
    assert client.get("/health").json()["ws_port"] == 47845
    assert client.get("/pet/../secret", follow_redirects=False).status_code in (403, 404)
    rebinding = TestClient(relay.app(), base_url="http://evil.example.invalid:47845")
    assert rebinding.get("/pet/index.html").status_code == 403
    assert seen["fetched"] == ["/pet/index.html"]


def test_the_relay_carries_only_an_overlay_with_the_nodes_credential(
    tmp_path: Path, tls: TlsMaterial
) -> None:
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    relay, seen = _relay(tmp_path, ca_file=tls.ca_file)  # TLS pinned to this authority only
    client = TestClient(relay.app(), base_url="http://127.0.0.1:47845")
    with client.websocket_connect(WS_URL) as ws:
        ws.send_text(_auth())
        reply = json.loads(ws.receive_text())
    assert reply["payload"]["ok"] is True
    url, kw, upstream = seen["upstream"][0]
    assert url == "wss://nox-core.example.invalid:47843/ws"
    assert kw["server_hostname"] == "nox-core.example.invalid"
    assert json.loads(upstream.sent[0])["payload"]["token"] == "n" * 43  # never the placeholder
    with pytest.raises(WebSocketDisconnect):  # not an overlay: refused
        with client.websocket_connect(WS_URL) as ws:
            ws.send_text(_auth("device"))
            ws.receive_text()
    with pytest.raises(WebSocketDisconnect):  # a web page elsewhere
        with client.websocket_connect(WS_URL, headers={"origin": "https://example.invalid"}):
            pass
    assert len(seen["upstream"]) == 1


def test_offline_or_switched_off_the_page_is_closed_and_retries(tmp_path: Path) -> None:
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    for relay, seen in (_relay(tmp_path, allowed=False), _relay(tmp_path, token=None)):
        client = TestClient(relay.app(), base_url="http://127.0.0.1:47845")
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect(WS_URL) as ws:
                ws.send_text(_auth())
                ws.receive_text()
        assert closed.value.code == 1013 and not seen["upstream"]


# ---- the core: overlay credentials --------------------------------------------------------------


def _tokens(store: OverlayTokenStore, may_hold: set[str]) -> TokenStore:
    tokens = TokenStore(overlay=store)
    tokens.add_node_overlay_check("test", lambda device: device in may_hold)
    return tokens


async def test_a_node_with_obs_gets_a_read_only_rotating_credential(tmp_path: Path) -> None:
    store = OverlayTokenStore(tmp_path / "overlay_tokens.json")
    disconnected: list[list[str]] = []
    hub, _ = voice_hub(overlay=store, disconnected=disconnected)
    session = fake_session()
    first = await hub.handle(session, p.M_OVERLAY_CREDENTIAL, p.OverlayCredentialRequest())
    assert first is not None and first["ok"]
    may_hold = {PI}
    tokens = _tokens(store, may_hold)
    # Through the network listener: only a node's overlay credential, and only as `overlay`.
    assert lan_accepts(tokens, first["token"])
    assert not lan_accepts(tokens, first["token"], "dashboard")
    _obs, obs_token = store.issue("obs")
    assert not lan_accepts(tokens, obs_token)
    assert tokens.authenticate(obs_token, "overlay", "overlay:x").ok  # OBS on the core itself
    # The hub asks again at every use: a node that may not hold one now gets nothing.
    may_hold.clear()
    assert not lan_accepts(tokens, first["token"])
    assert not lan_accepts(TokenStore(overlay=store), first["token"])  # no check installed: no
    failing = TokenStore(overlay=store)
    failing.add_node_overlay_check("test", lambda _device: 1 / 0)  # type: ignore[arg-type,return-value]
    assert not lan_accepts(failing, first["token"])  # a failing check: no
    may_hold.add(PI)
    tokens = _tokens(store, may_hold)
    again = await hub.handle(session, p.M_OVERLAY_CREDENTIAL, p.OverlayCredentialRequest())
    assert again == {"ok": False, "reason": "too_soon"}
    hub._nodes[PI].credential_at -= 120  # a minute later: a new one replaces the old
    second = await hub.handle(session, p.M_OVERLAY_CREDENTIAL, p.OverlayCredentialRequest())
    assert second is not None and second["ok"] and disconnected
    assert not lan_accepts(tokens, first["token"])
    assert await hub.revoke_overlay(PI, "node removed") == 1
    assert not lan_accepts(tokens, second["token"])
    assert node_overlay_label(PI) not in {c.label for c in store.credentials()}


async def test_no_obs_no_credential(tmp_path: Path) -> None:
    store = OverlayTokenStore(tmp_path / "overlay_tokens.json")
    hub, _ = voice_hub(overlay=store, offers={(PI, "voice.say")})
    answer = await hub.handle(fake_session(), p.M_OVERLAY_CREDENTIAL, p.OverlayCredentialRequest())
    assert answer == {"ok": False, "reason": "obs_not_enabled"}
    hub, _ = voice_hub(overlay=store, may_hold=set())  # offered, but narrowed meanwhile
    answer = await hub.handle(fake_session(), p.M_OVERLAY_CREDENTIAL, p.OverlayCredentialRequest())
    assert answer == {"ok": False, "reason": "obs_not_enabled"} and not store.credentials()


async def test_a_restarted_core_sweeps_stale_node_credentials(tmp_path: Path) -> None:
    store = OverlayTokenStore(tmp_path / "overlay_tokens.json")
    store.issue(node_overlay_label(PI))  # from an earlier run: PI may still hold it
    store.issue(node_overlay_label(PC))  # from an earlier run: PC was narrowed, offline, removed
    store.issue("obs")  # the core's own OBS overlay: not a node's, never swept
    reopened = OverlayTokenStore(tmp_path / "overlay_tokens.json")  # the restart
    hub, _ = voice_hub(overlay=reopened, may_hold={PI})
    assert await hub.sweep() == 1
    assert {c.label for c in reopened.credentials()} == {node_overlay_label(PI), "obs"}
    hub, _ = voice_hub(overlay=reopened, may_hold={PI})
    hub._overlay_allowed = lambda _device: 1 / 0  # type: ignore[assignment,return-value]
    assert await hub.sweep() == 1  # cannot tell: revoked (fail closed)
    assert {c.label for c in reopened.credentials()} == {"obs"}


async def test_narrowing_obs_or_removing_the_node_revokes_its_overlay_while_offline(
    nodes: NodeRepository, pairing: NodePairing, tmp_path: Path
) -> None:
    from nox.node.hub.registry import NodeRegistry
    from tests.unit.node.support import pair_node

    store = OverlayTokenStore(tmp_path / "overlay_tokens.json")
    registry = NodeRegistry(pairing, nodes)
    hub, _ = voice_hub(overlay=store)

    async def narrowed(device_id: str, group: str, on: bool) -> None:
        if group == "obs" and on:
            await hub.revoke_overlay(device_id, "obs narrowed")

    async def removed(device_id: str, reason: str) -> None:
        await hub.revoke_overlay(device_id, reason)

    registry.on_narrowed.append(narrowed)
    pairing.on_revoked(removed)
    laptop = (await pair_node(pairing, tmp_path, name="Stream PC")).node_id
    store.issue(node_overlay_label(laptop))
    await registry.set_narrowed(laptop, "obs", True)  # the node is not connected
    assert not store.credentials()
    store.issue(node_overlay_label(laptop))
    assert await pairing.revoke(laptop, reason="test")
    assert not store.credentials()


async def test_a_reconnecting_node_gets_a_fresh_credential_at_once(tmp_path: Path) -> None:
    store = OverlayTokenStore(tmp_path / "overlay_tokens.json")
    hub, _ = voice_hub(overlay=store)
    first, second = fake_session(), fake_session()
    assert (await hub.handle(first, p.M_OVERLAY_CREDENTIAL, p.OverlayCredentialRequest()))["ok"]
    again = await hub.handle(first, p.M_OVERLAY_CREDENTIAL, p.OverlayCredentialRequest())
    assert again == {"ok": False, "reason": "too_soon"}
    fresh = await hub.handle(second, p.M_OVERLAY_CREDENTIAL, p.OverlayCredentialRequest())
    assert fresh is not None and fresh["ok"]


async def test_the_credential_asks_are_capped_before_anything_else(tmp_path: Path) -> None:
    store = OverlayTokenStore(tmp_path / "overlay_tokens.json")
    hub, _ = voice_hub(overlay=store, may_hold=set())
    answers = [
        await hub.handle(fake_session(), p.M_OVERLAY_CREDENTIAL, p.OverlayCredentialRequest())
        for _ in range(4)
    ]
    assert [a["reason"] for a in answers if a] == ["obs_not_enabled"] * 3 + ["too_many"]

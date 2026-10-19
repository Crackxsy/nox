"""The network listener's HTTP side (`nox.lan.app`): what it serves, what it refuses."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography import x509
from starlette.testclient import TestClient

from nox.lan.app import (
    MAX_PAIR_BODY_BYTES,
    PairLimiter,
    _close_code,
    content_security_policy,
    create_lan_app,
    host_of,
)
from nox.lan.devices import LanDevices
from nox.lan.tls import TlsMaterial

BASE = "https://noxpi.local:47843"
ORIGIN = {"Origin": BASE}
#: A phone in the home network.
HOME_PEER = ("192.168.1.30", 50000)


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    folder = tmp_path / "dist"
    folder.mkdir()
    (folder / "index.html").write_text("<!doctype html><title>Nox</title>", encoding="utf-8")
    return folder


@pytest.fixture
def limiter() -> PairLimiter:
    return PairLimiter(per_address=3, total=30)


def make_app(
    devices: LanDevices,
    tls: TlsMaterial,
    dist: Path | None,
    limiter: PairLimiter,
    *,
    allow_public_clients: bool = False,
) -> object:
    return create_lan_app(
        devices=devices,
        tls=lambda: tls,
        hub_url=lambda: "ws://127.0.0.1:1/ws",
        bridge_secret=lambda: "secret",
        dashboard_dist=dist,
        limiter=limiter,
        allow_public_clients=allow_public_clients,
    )


def visit(app: object, *, base: str = BASE, peer: tuple[str, int] = HOME_PEER) -> TestClient:
    return TestClient(app, base_url=base, client=peer)  # type: ignore[arg-type]


@pytest.fixture
def client(devices: LanDevices, tls: TlsMaterial, dist: Path, limiter: PairLimiter) -> TestClient:
    return visit(make_app(devices, tls, dist, limiter))


# ---- who is answered at all ------------------------------------------------------------------


@pytest.mark.parametrize("peer", ["8.8.8.8", "100.64.0.1", "2001:db8::1", "testclient"])
def test_a_client_outside_the_home_network_gets_nothing(
    devices: LanDevices, tls: TlsMaterial, dist: Path, limiter: PairLimiter, peer: str
) -> None:
    outside = visit(make_app(devices, tls, dist, limiter), peer=(peer, 50000))
    for path in ("/dashboard/", "/nox-ca.crt", "/api/lan/info"):
        assert outside.get(path).status_code == 403, (peer, path)
    assert outside.post("/api/pair", json={"code": "ABCDEFGH"}, headers=ORIGIN).status_code == 403


@pytest.mark.parametrize("peer", ["127.0.0.1", "10.0.0.7", "172.16.0.1", "192.168.7.20", "fe80::1"])
def test_loopback_and_private_networks_are_answered(
    devices: LanDevices, tls: TlsMaterial, dist: Path, limiter: PairLimiter, peer: str
) -> None:
    inside = visit(make_app(devices, tls, dist, limiter), peer=(peer, 50000))
    assert inside.get("/dashboard/").status_code == 200


def test_public_clients_only_when_asked_for(
    devices: LanDevices, tls: TlsMaterial, dist: Path, limiter: PairLimiter
) -> None:
    app = make_app(devices, tls, dist, limiter, allow_public_clients=True)
    assert visit(app, peer=("8.8.8.8", 50000)).get("/dashboard/").status_code == 200


# ---- which host ------------------------------------------------------------------------------


def test_an_unknown_host_gets_a_page_with_the_right_addresses(client: TestClient) -> None:
    """DNS rebinding is turned away; a person who typed an old address learns the right one."""
    response = client.get("/dashboard/", headers={"Host": "evil.example"})
    assert response.status_code == 421
    assert "https://192.168.1.20:" in response.text and "/dashboard/" in response.text
    assert "Nox antwortet unter dieser Adresse nicht" in response.text
    assert "evil.example" not in response.text


def test_every_name_on_the_certificate_is_accepted(client: TestClient, tls: TlsMaterial) -> None:
    for host in (*tls.names, *tls.addresses):
        shown = f"[{host}]" if ":" in host else host
        response = client.get("/dashboard/", headers={"Host": f"{shown}:47843"})
        assert response.status_code == 200, host


@pytest.mark.parametrize(
    "host",
    [
        "noxpi.local:47843; script-src *",
        "noxpi.local:47843 x",
        "noxpi.local:99999",
        "noxpi.local:abc",
        "noxpi.local:",
        "[::1]x:5",
        "",
    ],
)
def test_a_malformed_host_is_refused(client: TestClient, host: str) -> None:
    assert client.get("/dashboard/", headers={"Host": host}).status_code == 421


@pytest.mark.parametrize(
    ("header", "host"),
    [
        ("noxpi.local:47843", "noxpi.local"),
        ("NOXPI.LOCAL", "noxpi.local"),
        ("[::1]:47843", "::1"),
        ("[::1]", "::1"),
        ("[::1]x:5", ""),
        ("192.168.1.20", "192.168.1.20"),
    ],
)
def test_host_of(header: str, host: str) -> None:
    assert host_of(header) == host


# ---- what every response carries -------------------------------------------------------------


def test_responses_carry_a_strict_policy(client: TestClient) -> None:
    response = client.get("/dashboard/")
    csp = response.headers["content-security-policy"]
    for directive in (
        "default-src 'self'",
        "script-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "connect-src 'self' wss://noxpi.local:47843",
    ):
        assert directive in csp, directive
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "hostile",
    [
        "noxpi.local; script-src *",  # ends the directive and opens a new one
        "noxpi.local *",  # adds a second source
        "noxpi.local\r\nset-cookie: x=1",  # header splitting
        "*.local",
        "https://evil.example",
        "noxpi.local'",
        "fe80::1%eth0",  # an IPv6 scope is not part of an origin
        "",
    ],
)
def test_the_policy_builder_refuses_anything_but_a_host(hostile: str) -> None:
    """A second line behind the Host check: even a value that got past it can never shape the
    policy. Before, the builder interpolated whatever it was given."""
    with pytest.raises(ValueError):
        content_security_policy(hostile, 47843)


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_the_policy_builder_refuses_a_port_out_of_range(port: int) -> None:
    with pytest.raises(ValueError):
        content_security_policy("noxpi.local", port)


@pytest.mark.parametrize(
    ("host", "port", "origin"),
    [
        ("noxpi.local", 47843, "wss://noxpi.local:47843"),
        ("192.168.1.20", None, "wss://192.168.1.20"),
        ("fe80::0:1", 47843, "wss://[fe80::1]:47843"),
    ],
)
def test_the_policy_builder_writes_one_origin(host: str, port: int | None, origin: str) -> None:
    csp = content_security_policy(host, port)
    assert f"connect-src 'self' {origin};" in csp
    assert csp.count("connect-src") == 1 and csp.count("script-src") == 1


def test_the_policy_is_built_from_the_validated_host_only(client: TestClient) -> None:
    """The raw header never reaches the policy: an upper-case host is accepted (hosts are case
    insensitive) but appears normalised."""
    headers = client.get("/dashboard/", headers={"Host": "NOXPI.LOCAL:47843"}).headers
    csp = headers["content-security-policy"]
    assert "wss://noxpi.local:47843" in csp and "NOXPI" not in csp


def test_an_ipv6_host_keeps_its_brackets_in_the_policy(
    devices: LanDevices, dist: Path, limiter: PairLimiter, tmp_path: Path
) -> None:
    from nox.lan.tls import ensure_tls

    material = ensure_tls(tmp_path / "tls6", names=("localhost",), addresses=("127.0.0.1", "::1"))
    app = make_app(devices, material, dist, limiter)
    response = visit(app, base="https://[::1]:47843").get("/dashboard/")
    assert response.status_code == 200
    assert "wss://[::1]:47843" in response.headers["content-security-policy"]


# ---- what is served --------------------------------------------------------------------------


def test_the_session_token_surface_does_not_exist_here(client: TestClient) -> None:
    for path in ("/api/state", "/api/providers", "/pet/", "/health"):
        assert client.get(path).status_code == 404, path


def test_the_root_leads_to_the_dashboard(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302 and response.headers["location"] == "/dashboard/"


def test_the_authority_can_be_downloaded_and_holds_no_key(
    client: TestClient, tls: TlsMaterial
) -> None:
    response = client.get("/nox-ca.crt")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-x509-ca-cert"
    assert response.headers["content-disposition"] == 'attachment; filename="nox-home-ca.crt"'
    assert response.content == tls.ca_der
    served = x509.load_der_x509_certificate(response.content)
    assert served.subject == x509.load_pem_x509_certificate(tls.ca_file.read_bytes()).subject
    assert b"PRIVATE KEY" not in response.content


def test_info_shows_both_fingerprints_and_no_loopback(client: TestClient, tls: TlsMaterial) -> None:
    body = client.get("/api/lan/info").json()
    assert body["fingerprint"] == tls.server_fingerprint
    assert body["ca_fingerprint"] == tls.fingerprint
    assert body["fingerprint"] != body["ca_fingerprint"]
    assert "127.0.0.1" not in body["addresses"] and "localhost" not in body["names"]


def test_a_missing_dashboard_says_so(
    devices: LanDevices, tls: TlsMaterial, limiter: PairLimiter
) -> None:
    assert visit(make_app(devices, tls, None, limiter)).get("/dashboard/").status_code == 503


# ---- pairing ---------------------------------------------------------------------------------


async def test_pairing_returns_the_token_once(client: TestClient, devices: LanDevices) -> None:
    start = await devices.start()
    response = client.post(
        "/api/pair",
        json={"code": start.code, "name": "Tablet", "default_name": "iPad"},
        headers=ORIGIN,
    )
    assert response.status_code == 201
    body = response.json()
    assert body["client_id"] == f"device:{body['device_id']}"
    assert body["name"] == "Tablet"
    verified = devices.verify(body["token"])
    assert verified is not None and verified.id == body["device_id"]
    again = client.post("/api/pair", json={"code": start.code}, headers=ORIGIN)
    assert again.status_code == 401 and again.json() == {"error": "used"}


@pytest.mark.parametrize(
    ("chosen_on_pc", "typed", "guess", "expected"),
    [
        ("Kitchen tablet", "", "iPad", "Kitchen tablet"),
        ("Kitchen tablet", "Mine", "iPad", "Mine"),
        ("", "", "iPad", "iPad"),
        ("", "", "", "Device"),
    ],
)
async def test_the_name_typed_on_the_device_then_the_pc_then_the_guess(
    client: TestClient,
    devices: LanDevices,
    chosen_on_pc: str,
    typed: str,
    guess: str,
    expected: str,
) -> None:
    start = await devices.start(name=chosen_on_pc)
    body = client.post(
        "/api/pair",
        json={"code": start.code, "name": typed, "default_name": guess},
        headers=ORIGIN,
    ).json()
    assert body["name"] == expected


@pytest.mark.parametrize(
    "headers",
    [{}, {"Origin": "https://evil.example"}, {"Origin": "http://noxpi.local:47843"}],
    ids=["no-origin", "foreign", "plain-http"],
)
async def test_pairing_only_from_this_origin(
    client: TestClient, devices: LanDevices, headers: dict[str, str]
) -> None:
    start = await devices.start()
    response = client.post("/api/pair", json={"code": start.code}, headers=headers)
    assert response.status_code == 403
    # The refused attempt did not use the code up.
    assert client.post("/api/pair", json={"code": start.code}, headers=ORIGIN).status_code == 201


def test_wrong_codes_run_into_the_limit(client: TestClient) -> None:
    statuses = [
        client.post("/api/pair", json={"code": "ZZZZZZZZ"}, headers=ORIGIN).status_code
        for _ in range(4)
    ]
    assert statuses == [401, 401, 401, 429]


def test_malformed_and_oversized_bodies_are_refused(
    devices: LanDevices, tls: TlsMaterial, dist: Path
) -> None:
    # Every attempt counts against the limit before the body is read; room for all of them here.
    client = visit(make_app(devices, tls, dist, PairLimiter(per_address=10, total=10)))
    assert client.post("/api/pair", content=b"{", headers=ORIGIN).status_code == 400
    assert client.post("/api/pair", json={"code": "x" * 40}, headers=ORIGIN).status_code == 400
    big = b"[" * 5000 + b"]" * 5000
    assert len(big) > MAX_PAIR_BODY_BYTES
    assert client.post("/api/pair", content=big, headers=ORIGIN).status_code == 413
    deep = b'{"code":' + b"[" * 400 + b"]" * 400 + b"}"
    assert len(deep) <= MAX_PAIR_BODY_BYTES
    assert client.post("/api/pair", content=deep, headers=ORIGIN).status_code == 400


def test_the_limiter_forgets_after_a_minute_and_not_a_moment_before() -> None:
    now = [0.0]
    limiter = PairLimiter(per_address=2, total=3, clock=lambda: now[0])
    assert limiter.allow("a") and limiter.allow("a") and not limiter.allow("a")
    assert limiter.allow("b")
    assert not limiter.allow("c")  # the total cap
    now[0] = 59.9
    assert not limiter.allow("a")
    now[0] = 60.0  # the window is exactly one minute
    assert limiter.allow("a") and limiter.allow("c")


def test_the_limiter_forgets_quiet_addresses() -> None:
    now = [0.0]
    limiter = PairLimiter(per_address=1, total=1000, clock=lambda: now[0])
    for i in range(50):
        limiter.allow(f"10.0.0.{i}")
    now[0] = 61.0
    limiter.allow("10.0.1.1")
    assert set(limiter._by_address) == {"10.0.1.1"}


# ---- the WebSocket bridge's close codes -----------------------------------------------------


@pytest.mark.parametrize(
    ("given", "sent"),
    [
        (None, 1000),
        (1000, 1000),
        (1008, 1008),
        (4001, 4001),
        (1005, 1011),
        (1006, 1011),
        (1015, 1011),
        (999, 1011),
    ],
)
def test_only_sendable_close_codes_reach_the_browser(given: int | None, sent: int) -> None:
    assert _close_code(given) == sent


# ---- the WebSocket bridge ----------------------------------------------------------------------


def _ws_headers(host: str = "noxpi.local:47843") -> dict[str, str]:
    return {"host": host, "origin": BASE}


def test_a_websocket_under_a_foreign_host_is_refused(client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect("/ws", headers=_ws_headers("evil.example")) as ws:
            ws.receive_text()
    assert refused.value.code == 1008


def test_an_unreachable_hub_closes_with_1011_after_the_first_frame(client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    with client.websocket_connect("/ws", headers=_ws_headers()) as ws:
        ws.send_text('{"name": "ipc.auth"}')
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 1011


def test_silent_websockets_are_capped_per_address(
    devices: LanDevices, tls: TlsMaterial, dist: Path, limiter: PairLimiter
) -> None:
    """Sockets that never authenticate cost the hub nothing, and only so many may wait."""
    import contextlib

    from starlette.websockets import WebSocketDisconnect

    from nox.lan.app import MAX_PREAUTH_PER_ADDRESS

    client = visit(make_app(devices, tls, dist, limiter))
    with contextlib.ExitStack() as stack:
        for _ in range(MAX_PREAUTH_PER_ADDRESS):
            stack.enter_context(client.websocket_connect("/ws", headers=_ws_headers()))
        with pytest.raises(WebSocketDisconnect) as refused:
            with client.websocket_connect("/ws", headers=_ws_headers()) as ws:
                ws.receive_text()
        assert refused.value.code == 1013

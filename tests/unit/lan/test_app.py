"""The network listener's HTTP side (`nox.lan.app`): what it serves, what it refuses."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography import x509
from starlette.testclient import TestClient

from nox.lan.app import PairLimiter, create_lan_app, host_of
from nox.lan.devices import LanDevices
from nox.lan.tls import TlsMaterial

BASE = "https://noxpi.local:47843"
ORIGIN = {"Origin": BASE}


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    folder = tmp_path / "dist"
    folder.mkdir()
    (folder / "index.html").write_text("<!doctype html><title>Nox</title>", encoding="utf-8")
    return folder


@pytest.fixture
def limiter() -> PairLimiter:
    return PairLimiter(per_address=3, total=30)


@pytest.fixture
def client(devices: LanDevices, tls: TlsMaterial, dist: Path, limiter: PairLimiter) -> TestClient:
    app = create_lan_app(
        devices=devices,
        tls=lambda: tls,
        hub_url=lambda: "ws://127.0.0.1:1/ws",
        bridge_secret=lambda: "secret",
        dashboard_dist=dist,
        limiter=limiter,
    )
    return TestClient(app, base_url=BASE)


def test_an_unknown_host_is_turned_away(client: TestClient) -> None:
    """DNS rebinding: a page on another domain pointed at this machine gets nothing."""
    response = client.get("/dashboard/", headers={"Host": "evil.example"})
    assert response.status_code == 421


def test_every_name_on_the_certificate_is_accepted(client: TestClient) -> None:
    for host in ("noxpi.local:47843", "192.168.1.20:47843", "localhost:47843"):
        assert client.get("/dashboard/", headers={"Host": host}).status_code == 200, host


def test_responses_carry_a_strict_policy(client: TestClient) -> None:
    response = client.get("/dashboard/")
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "wss://noxpi.local:47843" in csp
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_the_session_token_surface_does_not_exist_here(client: TestClient) -> None:
    for path in ("/api/state", "/api/providers", "/pet/", "/health"):
        assert client.get(path).status_code == 404, path


def test_the_root_leads_to_the_dashboard(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302 and response.headers["location"] == "/dashboard/"


def test_the_authority_can_be_downloaded(client: TestClient, tls: TlsMaterial) -> None:
    response = client.get("/nox-ca.crt")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-x509-ca-cert"
    assert x509.load_der_x509_certificate(response.content).public_bytes is not None
    assert response.content == tls.ca_der


def test_info_shows_what_a_person_compares(client: TestClient, tls: TlsMaterial) -> None:
    body = client.get("/api/lan/info").json()
    assert body["fingerprint"] == tls.fingerprint
    assert "127.0.0.1" not in body["addresses"] and "localhost" not in body["names"]


async def test_pairing_returns_the_token_once(client: TestClient, devices: LanDevices) -> None:
    start = await devices.start()
    response = client.post("/api/pair", json={"code": start.code, "name": "Tablet"}, headers=ORIGIN)
    assert response.status_code == 201
    body = response.json()
    assert body["client_id"] == f"device:{body['device_id']}"
    assert body["name"] == "Tablet"
    assert devices.verify(body["token"]) is not None
    again = client.post("/api/pair", json={"code": start.code}, headers=ORIGIN)
    assert again.status_code == 401 and again.json() == {"error": "used"}


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


def test_malformed_bodies_are_refused(client: TestClient) -> None:
    assert client.post("/api/pair", content=b"{", headers=ORIGIN).status_code == 400
    assert client.post("/api/pair", json={"code": "x" * 40}, headers=ORIGIN).status_code == 400


def test_the_limiter_forgets_after_a_minute() -> None:
    now = [0.0]
    limiter = PairLimiter(per_address=2, total=3, clock=lambda: now[0])
    assert limiter.allow("a") and limiter.allow("a") and not limiter.allow("a")
    assert limiter.allow("b")
    assert not limiter.allow("c")  # the total cap
    now[0] = 61.0
    assert limiter.allow("a") and limiter.allow("c")


@pytest.mark.parametrize(
    ("header", "host"),
    [
        ("noxpi.local:47843", "noxpi.local"),
        ("NOXPI.LOCAL", "noxpi.local"),
        ("[::1]:47843", "::1"),
        ("192.168.1.20", "192.168.1.20"),
    ],
)
def test_host_of(header: str, host: str) -> None:
    assert host_of(header) == host


def test_a_missing_dashboard_says_so(
    devices: LanDevices, tls: TlsMaterial, limiter: PairLimiter
) -> None:
    app = create_lan_app(
        devices=devices,
        tls=lambda: tls,
        hub_url=lambda: "ws://127.0.0.1:1/ws",
        bridge_secret=lambda: "secret",
        dashboard_dist=None,
        limiter=limiter,
    )
    assert TestClient(app, base_url=BASE).get("/dashboard/").status_code == 503


def test_the_policy_is_built_from_the_validated_host_only(client: TestClient) -> None:
    """The raw header never reaches the policy: an upper-case host is accepted (hosts are case
    insensitive) but appears normalised, and a host with anything after the port is refused."""
    response = client.get("/dashboard/", headers={"Host": "NOXPI.LOCAL:47843"})
    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert "wss://noxpi.local:47843" in csp
    assert "NOXPI" not in csp


@pytest.mark.parametrize(
    "host",
    ["noxpi.local:47843; script-src *", "noxpi.local:47843 x", "noxpi.local:99999", "noxpi.local:"],
)
def test_a_host_with_a_bad_port_is_refused(client: TestClient, host: str) -> None:
    assert client.get("/dashboard/", headers={"Host": host}).status_code == 421


def test_an_ipv6_loopback_host_keeps_its_brackets_in_the_policy(
    devices: LanDevices, dist: Path, limiter: PairLimiter, tmp_path: Path
) -> None:
    from nox.lan.tls import ensure_tls

    material = ensure_tls(tmp_path / "tls6", names=("localhost",), addresses=("127.0.0.1", "::1"))
    app = create_lan_app(
        devices=devices,
        tls=lambda: material,
        hub_url=lambda: "ws://127.0.0.1:1/ws",
        bridge_secret=lambda: "secret",
        dashboard_dist=dist,
        limiter=limiter,
    )
    response = TestClient(app, base_url="https://[::1]:47843").get("/dashboard/")
    assert response.status_code == 200
    assert "wss://[::1]:47843" in response.headers["content-security-policy"]

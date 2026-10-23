"""Claiming a Nox-OS device (`nox.noxos.claim`, step O3): while unclaimed the listener serves the
claim and nothing else, the claim needs the code and the open setup window (the root helper's
verdict), pairs the claiming browser as the owner's device, sets the PIN the setup portal took,
and a refused claim leaves no device behind."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from nox.lan.app import MAX_CLAIM_BODY_BYTES, PairLimiter, create_lan_app
from nox.lan.devices import LanDevices
from nox.lan.tls import TlsMaterial
from nox.noxos.claim import (
    ONBOARDING_URL,
    STRINGS,
    ClaimGate,
    ClaimService,
    asset,
    normalize_claim_code,
)
from nox.noxos.helper import HelperError
from nox.security.secrets import InMemorySecretStore, PinManager

BASE = "https://noxpi.local:47843"
ORIGIN = {"Origin": BASE}
HOME_PEER = ("192.168.1.30", 50000)
#: Fictitious, in the device's own format.
CODE = "ABCD-EFGH-JKMN"
PASS = "correct horse battery staple"  # fictitious


class Audit:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def append(self, **row: Any) -> int:
        self.rows.append(row)
        return len(self.rows)


class Helper:
    """The root helper's `status` and `claim` as the core sees them."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.setup_mode = True
        self.claimed = False
        self.passphrase = False
        self.pending_pin: str | None = None
        self.pending_passphrase: str | None = None
        self.set_passphrase: str | None = None
        self.wait = False
        self.locked = False
        self.ticket: str | None = None
        self.code = normalize_claim_code(CODE)
        self.down = False

    def _status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "setup_mode": self.setup_mode,
            "setup_seconds_left": 1500 if self.setup_mode else None,
            "claimed": self.claimed,
            "claim_code": "device",
            "pending_pin": self.pending_pin is not None,
            "pending_passphrase": self.pending_passphrase is not None,
            "passphrase": self.passphrase,
        }

    async def __call__(self, lines: list[str]) -> dict[str, Any]:
        self.calls.append(lines)
        if self.down:
            raise HelperError("the Nox-OS helper is not reachable")
        if lines == ["status"]:
            return self._status()
        verb, device, address = lines[0].split(" ")
        assert verb == "claim" and device and address == HOME_PEER[0]
        if self.wait:
            raise HelperError("wait 4 s", refused=True, code="claim_wait")
        if self.claimed:
            raise HelperError("owned", refused=True, code="claimed")
        if not self.setup_mode:
            raise HelperError("closed", refused=True, code="not_setup")
        code = base64.b64decode(lines[1].removeprefix("code ")).decode()
        if code != self.code:
            raise HelperError("wrong claim code", refused=True, code="claim_wrong")
        extra = {
            line.split(" ")[0]: base64.b64decode(line.split(" ")[1]).decode() for line in lines[2:]
        }
        self.ticket = extra.get("ticket")
        if self.locked and self.ticket is None:
            raise HelperError("locked", refused=True, code="claim_locked_lan")
        if "new" in extra:
            self.set_passphrase = extra["new"]
        elif self.pending_passphrase is not None:
            self.set_passphrase = self.pending_passphrase
        self.passphrase = self.set_passphrase is not None
        self.pending_passphrase = None
        self.claimed, self.setup_mode = True, False
        pin, self.pending_pin = self.pending_pin, None
        answer = self._status()
        answer["pin"] = base64.b64encode(pin.encode()).decode() if pin else None
        return answer


class Gate(ClaimGate):
    def __init__(self) -> None:
        super().__init__()
        self.os = True

    @property
    def nox_os(self) -> bool:
        return self.os

    def claimed(self) -> bool:
        return self._claimed


@pytest.fixture
def helper() -> Helper:
    return Helper()


@pytest.fixture
def pin() -> PinManager:
    return PinManager(InMemorySecretStore(), prefer_argon2=False)


@pytest.fixture
def audit() -> Audit:
    return Audit()


@pytest.fixture
def service(
    devices: LanDevices, tls: TlsMaterial, helper: Helper, pin: PinManager, audit: Audit
) -> ClaimService:
    return ClaimService(
        gate=Gate(),
        devices=devices,
        helper=helper,
        pin=pin,
        audit=audit,  # type: ignore[arg-type]
        tls=lambda: tls,
        limiter=PairLimiter(per_address=5, total=20),
    )


@pytest.fixture
def client(
    devices: LanDevices, tls: TlsMaterial, tmp_path: Path, service: ClaimService
) -> TestClient:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>Nox</title>", encoding="utf-8")
    app = create_lan_app(
        devices=devices,
        tls=lambda: tls,
        hub_url=lambda: "ws://127.0.0.1:1/ws",
        bridge_secret=lambda: "secret",
        dashboard_dist=dist,
        limiter=PairLimiter(per_address=3, total=30),
        claim=service.surface(),
    )
    return TestClient(app, base_url=BASE, client=HOME_PEER)


def claim(client: TestClient, **body: Any) -> Any:
    payload = {"code": CODE, "name": "Phone", "pin": "135790", "passphrase": PASS, **body}
    return client.post("/api/claim", json=payload, headers=ORIGIN)


# ---- the gate ------------------------------------------------------------------------------


def test_the_gate_is_inactive_off_nox_os_and_fails_closed_on_it(tmp_path: Path) -> None:
    release, marker = tmp_path / "nox-os-release", tmp_path / "claimed"
    gate = ClaimGate(release=release, marker=marker)
    assert gate.required() is False  # a PC: no claim at all
    release.write_text("NOX_OS_VERSION=v0.7.0\n", "ascii")
    assert gate.required() is True  # Nox-OS, no mark: unclaimed
    me = marker.parent.stat().st_uid
    owned = ClaimGate(release=release, marker=marker, root_uid=me + 1)
    marker.write_text("{}", "ascii")
    assert owned.required() is True  # a mark that is not root's counts for nothing
    gate = ClaimGate(release=release, marker=marker, root_uid=me)
    assert gate.required() is False
    marker.unlink()
    assert gate.required() is False  # once claimed, never back


# ---- while unclaimed, only the claim --------------------------------------------------------


def test_an_unclaimed_device_serves_the_claim_and_nothing_else(client: TestClient) -> None:
    root = client.get("/", follow_redirects=False)
    assert (root.status_code, root.headers["location"]) == (302, "/claim/")
    page = client.get("/dashboard/", follow_redirects=False)
    assert (page.status_code, page.headers["location"]) == (302, "/claim/")
    assert client.post("/api/pair", json={"code": "ABCDEFGH"}, headers=ORIGIN).status_code == 403
    assert client.post("/api/node/pair", json={}).json() == {"error": "unclaimed"}
    with pytest.raises(Exception):  # noqa: B017,PT011 - the socket is closed before any accept
        with client.websocket_connect("/ws", headers=ORIGIN):
            pass
    assert client.get("/api/lan/info").status_code == 200
    assert client.get("/nox-ca.crt").status_code == 200
    claim_page = client.get("/claim/")
    assert claim_page.status_code == 200 and "/claim/claim.js" in claim_page.text
    assert "script-src 'self'" in claim_page.headers["content-security-policy"]
    assert client.get("/claim/claim.js").headers["content-type"].startswith("text/javascript")
    assert client.get("/claim/nope").status_code == 404


def test_the_claim_status_names_what_is_needed_and_no_secret(
    client: TestClient, tls: TlsMaterial, helper: Helper
) -> None:
    status = client.get("/api/claim").json()
    assert status["claimed"] is False and status["window_open"] is True
    assert status["needs_pin"] is True and status["needs_passphrase"] is True
    assert status["ca_fingerprint"] == tls.fingerprint
    assert CODE not in json.dumps(status)
    helper.pending_pin, helper.passphrase = "135790", True
    status = client.get("/api/claim").json()
    assert status["needs_pin"] is False and status["needs_passphrase"] is False
    helper.down = True
    assert client.get("/api/claim").json()["available"] is False


# ---- the claim -----------------------------------------------------------------------------


def test_the_claim_pairs_the_owner_sets_pin_and_passphrase_and_opens_everything(
    client: TestClient,
    devices: LanDevices,
    helper: Helper,
    pin: PinManager,
    audit: Audit,
) -> None:
    answer = claim(client, code="abcd efgh jkmn")  # typed loosely
    assert answer.status_code == 201, answer.text
    body = answer.json()
    assert body["next"] == ONBOARDING_URL and body["client_id"] == f"device:{body['device_id']}"
    assert devices.verify(body["token"]) is not None
    assert pin.is_set() and pin.verify_pin("135790").ok
    sent = helper.calls[-1]
    assert sent[0] == f"claim {body['device_id']} {HOME_PEER[0]}"
    assert base64.b64decode(sent[2].removeprefix("new ")).decode() == PASS
    assert [r["result"] for r in audit.rows if r["action"] == "noxos.claim"] == ["ok"]
    assert PASS not in json.dumps(audit.rows) and "135790" not in json.dumps(audit.rows)
    # Claimed: the dashboard is served, the claim is over.
    assert client.get("/dashboard/").status_code == 200
    assert client.get("/api/claim").json() == {"claimed": True}
    assert claim(client).status_code == 409
    again = client.get("/claim/", follow_redirects=False)
    assert again.headers["location"] == "/dashboard/"


def test_what_the_setup_portal_kept_is_used_unless_the_claimant_sets_others(
    client: TestClient, helper: Helper, pin: PinManager
) -> None:
    helper.pending_pin, helper.pending_passphrase = "246802", "the portal's passphrase"
    status = client.get("/api/claim").json()
    assert status["needs_pin"] is False and status["needs_passphrase"] is False
    assert status["staged_pin"] is True and status["staged_passphrase"] is True
    answer = client.post("/api/claim", json={"code": CODE, "name": "Phone"}, headers=ORIGIN)
    assert answer.status_code == 201, answer.text
    assert len(helper.calls[-1]) == 2  # nothing sent: the kept ones apply
    assert pin.verify_pin("246802").ok
    assert helper.set_passphrase == "the portal's passphrase"


def test_the_claimant_overrides_what_the_setup_portal_kept(
    client: TestClient, helper: Helper, pin: PinManager
) -> None:
    helper.pending_pin, helper.pending_passphrase = "246802", "the portal's passphrase"
    answer = claim(client)  # with its own PIN and passphrase
    assert answer.status_code == 201, answer.text
    assert pin.verify_pin("135790").ok and not pin.verify_pin("246802").ok
    assert helper.set_passphrase == PASS


def test_a_slowed_down_address_is_told_to_wait(
    client: TestClient, helper: Helper, devices: LanDevices
) -> None:
    helper.wait = True
    answer = claim(client)
    assert (answer.status_code, answer.json()) == (429, {"error": "wait"})
    assert [d for d in devices.devices() if not d.revoked] == []


@pytest.mark.parametrize(
    ("change", "status", "error"),
    [
        ({"code": "ZZZZ-ZZZZ-ZZZZ"}, 401, "wrong_code"),
        ({"code": "not a code!"}, 401, "wrong_code"),
        ({"pin": "12"}, 400, "pin_required"),
        ({"passphrase": "short"}, 400, "passphrase_required"),
    ],
)
def test_a_refused_claim_leaves_no_device_and_no_owner(
    client: TestClient,
    devices: LanDevices,
    pin: PinManager,
    change: dict[str, str],
    status: int,
    error: str,
) -> None:
    answer = claim(client, **change)
    assert (answer.status_code, answer.json()) == (status, {"error": error})
    assert [d for d in devices.devices() if not d.revoked] == []
    assert not pin.is_set()
    assert client.get("/dashboard/", follow_redirects=False).status_code == 302


def test_outside_the_setup_window_nobody_claims(client: TestClient, helper: Helper) -> None:
    helper.setup_mode = False
    answer = claim(client)
    assert (answer.status_code, answer.json()) == (403, {"error": "window_closed"})
    helper.setup_mode, helper.down = True, True
    assert claim(client).status_code == 503


def test_the_claim_is_same_origin_small_and_rate_limited(client: TestClient) -> None:
    assert client.post("/api/claim", json={"code": CODE}).status_code == 403  # no Origin
    evil = {"Origin": "https://evil.example.invalid"}
    assert client.post("/api/claim", json={"code": CODE}, headers=evil).status_code == 403
    big = {"code": CODE, "name": "x" * MAX_CLAIM_BODY_BYTES}
    assert client.post("/api/claim", json=big, headers=ORIGIN).status_code == 413
    codes = [claim(client, code="ZZZZ-ZZZZ-ZZZZ").status_code for _ in range(6)]
    assert codes == [401] * 5 + [429]


def test_without_a_claim_surface_nothing_changes(devices: LanDevices, tls: TlsMaterial) -> None:
    app = create_lan_app(
        devices=devices,
        tls=lambda: tls,
        hub_url=lambda: "ws://127.0.0.1:1/ws",
        bridge_secret=lambda: "secret",
        dashboard_dist=None,
        limiter=PairLimiter(per_address=3, total=30),
    )
    plain = TestClient(app, base_url=BASE, client=HOME_PEER)
    assert plain.get("/claim/").status_code == 404
    assert plain.get("/", follow_redirects=False).headers["location"] == "/dashboard/"


# ---- the page ------------------------------------------------------------------------------


def test_the_claim_page_texts_exist_in_both_languages() -> None:
    assert set(STRINGS["de"]) == set(STRINGS["en"])
    script = asset("claim.js")
    page = asset("")
    assert script is not None and page is not None
    text = page[0].decode()
    keys = set()
    for part in text.split('data-t="')[1:]:
        keys.add(part.split('"', 1)[0])
    assert keys <= set(STRINGS["en"])
    assert "<script>" not in text and "style=" not in text  # nothing inline (the CSP)
    assert "http://" not in text and "https://" not in text  # nothing from elsewhere
    for field in ("code", "name", "pin", "pin2", "passphrase", "passphrase2"):
        assert f'<label for="{field}"' in text, field
    assert 'role="alert"' in text and 'aria-live="polite"' in text


def test_claim_codes_normalize_like_the_root_side() -> None:
    assert normalize_claim_code("abcd-efgh-jkmn") == "ABCDEFGHJKMN"
    assert normalize_claim_code("ABCD EFGH JKMN") == "ABCDEFGHJKMN"
    assert normalize_claim_code("oooo-iiii-llll") == "000011111111"
    assert normalize_claim_code("ABCD") is None
    assert normalize_claim_code("ABCD-EFGH-JKM!") is None
    assert normalize_claim_code("ABCD-EFGH-JKMU") is None  # U is not in the alphabet


def test_the_setup_portals_ticket_goes_to_the_helper_and_a_locked_lan_is_said(
    client: TestClient, helper: Helper
) -> None:
    helper.locked = True
    answer = claim(client)
    assert (answer.status_code, answer.json()) == (429, {"error": "locked"})
    answer = claim(client, ticket="ticket-for-this-test-only-aaaaaa")
    assert answer.status_code == 201, answer.text
    assert helper.ticket == "ticket-for-this-test-only-aaaaaa"
    assert claim(client, ticket="bad ticket").status_code in (400, 409)

"""The Nox-OS setup portal (os/pi-gen/stage-nox/04-firstboot/files/nox_setup_portal.py): every
foreign address leads to it (captive portal), the form needs a token bound to the phone and the
time and same-origin headers, requests are limited, nothing is written for a bad form, and the
texts exist in German and English. A real server on loopback, headless, stopped after each
test. Only fictitious data."""

from __future__ import annotations

import http.client
import json
import threading
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.unit.nox_os.conftest import FIRSTBOOT, load_tool

portal_mod = load_tool("nox_setup_portal", FIRSTBOOT)

SSID = "Springfield-WLAN"  # fictitious
PSK = "springfield-example-psk"  # fictitious
PIN = "135790"
PASSPHRASE = "a long maintenance passphrase"  # fictitious
CODE = "ABCD-EFGH-JKMN"


@pytest.fixture
def state(tmp_path: Path) -> Any:
    setup = tmp_path / "setup"
    (setup / "portal").mkdir(parents=True)
    (tmp_path / "setup-mode").write_text(json.dumps({"v": 1, "until": 1800}), "utf-8")
    (setup / "networks.json").write_text(
        json.dumps(
            {"networks": [{"ssid": SSID, "open": False}, {"ssid": "Open-Cafe", "open": True}]}
        ),
        "utf-8",
    )
    (setup / "status.json").write_text(json.dumps({"state": "idle"}), "utf-8")
    (setup / "claim-code").write_text(CODE + "\n", "ascii")
    return portal_mod.State(
        setup_mode=tmp_path / "setup-mode",
        console=setup / "console.json",
        claim_code=setup / "claim-code",
        networks=setup / "networks.json",
        status=setup / "status.json",
        request=setup / "portal" / "request.json",
        uptime=lambda: 300.0,
        fingerprint=lambda: "AB:CD:EF:01:23:45:67:89",
    )


@pytest.fixture
def portal(state: Any) -> Iterator[Any]:
    portal = portal_mod.Portal(state=state, address="127.0.0.1", port=0)
    server = portal_mod.serve(portal)
    portal.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield portal
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(
    portal: Any,
    method: str,
    path: str,
    *,
    host: str | None = None,
    body: str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], str]:
    conn = http.client.HTTPConnection("127.0.0.1", portal.port, timeout=10)
    try:
        sent = {"Host": host or f"127.0.0.1:{portal.port}", **(headers or {})}
        if body is not None:
            sent.setdefault("Content-Type", "application/x-www-form-urlencoded")
        conn.request(method, path, body=body, headers=sent)
        answer = conn.getresponse()
        return answer.status, {k.lower(): v for k, v in answer.getheaders()}, answer.read().decode()
    finally:
        conn.close()


def token_of(page: str) -> str:
    return page.split('name="token" value="', 1)[1].split('"', 1)[0]


def post(portal: Any, fields: dict[str, str], *, headers: dict[str, str] | None = None) -> Any:
    _, _, page = request(portal, "GET", "/")
    data = {"token": token_of(page), "lang": "en", **fields}
    origin = {"Origin": portal.origin, "Sec-Fetch-Site": "same-origin"}
    return request(
        portal,
        "POST",
        "/setup",
        body=urllib.parse.urlencode(data),
        headers={**origin, **(headers or {})},
    )


GOOD = {
    "claim_code": CODE,
    "network": SSID,
    "psk": PSK,
    "pin": PIN,
    "pin2": PIN,
    "passphrase": PASSPHRASE,
    "passphrase2": PASSPHRASE,
}


@pytest.mark.parametrize(
    ("host", "path"),
    [
        ("connectivitycheck.gstatic.com", "/generate_204"),
        ("captive.apple.com", "/hotspot-detect.html"),
        ("www.msftconnecttest.com", "/connecttest.txt"),
        ("example.invalid", "/anything"),
    ],
)
def test_every_other_address_leads_to_the_portal(portal: Any, host: str, path: str) -> None:
    status, headers, _ = request(portal, "GET", path, host=host)
    assert status == 302 and headers["location"] == f"{portal.origin}/"


def test_the_form_is_strict_accessible_and_without_scripts(portal: Any) -> None:
    status, headers, page = request(portal, "GET", "/", headers={"Accept-Language": "de-DE,de"})
    assert status == 200
    csp = headers["content-security-policy"]
    assert (
        "default-src 'none'" in csp and "script-src" not in csp and "frame-ancestors 'none'" in csp
    )
    assert headers["cache-control"] == "no-store" and headers["x-frame-options"] == "DENY"
    assert '<html lang="de">' in page and "Nox einrichten" in page
    assert "<script" not in page and "style=" not in page and "https://" not in page
    for field in ("other_ssid", "psk", "pin", "pin2", "passphrase", "passphrase2"):
        assert f'<label for="{field}">' in page, field
    assert "<legend>" in page and SSID in page and "Open-Cafe" in page
    css_status, css_headers, _ = request(portal, "GET", "/portal.css")
    assert css_status == 200 and css_headers["content-type"].startswith("text/css")


def test_a_good_form_writes_one_request_for_root_and_shows_the_next_step(
    portal: Any, state: Any
) -> None:
    status, _, page = post(portal, GOOD)
    assert status == 200, page
    request_file = json.loads(state.request.read_text("utf-8"))
    ticket = request_file.pop("ticket")
    assert len(ticket) >= 32 and f"ticket={ticket}" in page  # in the claim link, once
    assert request_file == {
        "v": 1,
        "wifi": {"ssid": SSID, "psk": PSK, "hidden": False},
        "claim_code": CODE,
        "peer": "127.0.0.1",
        "pin": PIN,
        "passphrase": PASSPHRASE,
    }
    assert CODE in page and "https://nox.local:47843/claim/#code=ABCDEFGHJKMN" in page
    assert "AB:CD:EF:01:23:45:67:89" in page
    assert PSK not in page and PIN not in page


def test_the_cable_needs_no_wifi_and_an_open_network_no_password(portal: Any, state: Any) -> None:
    status, _, _ = post(portal, {**GOOD, "network": "__cable__", "psk": ""})
    assert status == 200
    assert json.loads(state.request.read_text("utf-8"))["wifi"] is None
    status, _, _ = post(portal, {**GOOD, "network": "Open-Cafe", "psk": ""})
    assert status == 200
    assert json.loads(state.request.read_text("utf-8"))["wifi"]["psk"] is None


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"network": ""}, "network"),
        ({"network": "Somebody-Else"}, "network"),
        ({"psk": ""}, "psk"),
        ({"psk": "short"}, "psk"),
        ({"network": "__other__", "other_ssid": ""}, "other_ssid"),
        ({"pin": "123", "pin2": "123"}, "pin"),
        ({"pin2": "246802"}, "pin2"),
        ({"passphrase": "too short", "passphrase2": "too short"}, "passphrase"),
        ({"passphrase2": "another long passphrase"}, "passphrase2"),
        ({"claim_code": ""}, "claim_code"),
        ({"claim_code": "ABC"}, "claim_code"),
    ],
)
def test_a_bad_form_writes_nothing_and_names_the_field(
    portal: Any, state: Any, change: dict[str, str], field: str
) -> None:
    status, _, page = post(portal, {**GOOD, **change})
    assert status == 400
    assert not state.request.exists()
    assert f'href="#{field}"' in page and 'role="alert"' in page
    assert 'aria-invalid="true"' in page
    assert PSK not in page and PASSPHRASE not in page  # secrets are never sent back


def test_the_claim_code_comes_first_filled_in_when_the_device_made_it(
    portal: Any, state: Any
) -> None:
    _, _, page = request(portal, "GET", "/")
    assert f'id="claim_code" name="claim_code" value="{CODE}"' in page
    assert page.index('id="claim_code"') < page.index('name="network"')
    state.claim_code.unlink()  # the card's code: never known to the portal
    _, _, page = request(portal, "GET", "/")
    assert 'id="claim_code" name="claim_code" aria-describedby' in page and CODE not in page
    status, _, done = post(portal, GOOD)
    assert status == 200 and CODE not in done  # not shown back either


def test_a_wrong_code_reported_by_root_is_shown(portal: Any, state: Any) -> None:
    state.status.write_text(json.dumps({"state": "idle", "error": "claim_wrong"}), "utf-8")
    _, _, page = request(portal, "GET", "/")
    assert "nothing was saved" in page


def test_once_the_secrets_are_set_the_portal_asks_only_for_wifi(portal: Any, state: Any) -> None:
    state.status.write_text(
        json.dumps(
            {
                "state": "failed",
                "error": "wrong_password",
                "tried_ssid": SSID,
                "security_done": True,
            }
        ),
        "utf-8",
    )
    _, _, page = request(portal, "GET", "/")
    assert 'id="pin"' not in page and "password was not accepted" in page
    status, _, _ = post(portal, {"claim_code": CODE, "network": SSID, "psk": PSK})
    assert status == 200
    assert "pin" not in json.loads(state.request.read_text("utf-8"))


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://example.invalid"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Content-Type": "application/json"},
    ],
)
def test_cross_site_posts_are_refused(portal: Any, state: Any, headers: dict[str, str]) -> None:
    status, _, _ = post(portal, GOOD, headers=headers)
    assert status == 403 and not state.request.exists()


def test_the_token_is_required_and_bound_to_the_phone_and_time() -> None:
    clock = {"now": 1000.0}
    tokens = portal_mod.Tokens(key=b"k" * 32, clock=lambda: clock["now"])
    token = tokens.make("10.42.0.20")
    assert tokens.check("10.42.0.20", token)
    assert not tokens.check("10.42.0.21", token)  # another phone
    assert not tokens.check("10.42.0.20", "")
    assert not tokens.check("10.42.0.20", token[:-1] + ("0" if token[-1] != "0" else "1"))
    clock["now"] += portal_mod.TOKEN_TTL_S + 1
    assert not tokens.check("10.42.0.20", token)  # expired


def test_a_post_without_a_token_is_refused(portal: Any, state: Any) -> None:
    body = urllib.parse.urlencode({**GOOD, "token": "1.abc"})
    status, _, _ = request(portal, "POST", "/setup", body=body, headers={"Origin": portal.origin})
    assert status == 403 and not state.request.exists()


def test_a_post_without_origin_is_refused(portal: Any, state: Any) -> None:
    _, _, page = request(portal, "GET", "/")
    body = urllib.parse.urlencode({**GOOD, "token": token_of(page)})
    status, _, _ = request(portal, "POST", "/setup", body=body)
    assert status == 403 and not state.request.exists()


def test_posts_are_rate_limited(portal: Any) -> None:
    codes = [post(portal, {**GOOD, "pin": "1"})[0] for _ in range(7)]
    assert codes == [400] * 6 + [429]


def test_after_the_window_the_portal_says_how_to_reopen_it(portal: Any, state: Any) -> None:
    state.setup_mode.unlink()
    status, _, page = request(portal, "GET", "/")
    assert status == 410 and "plug it in again" in page


def test_a_big_body_is_refused(portal: Any) -> None:
    body = "x" * (portal_mod.MAX_BODY + 1)
    status, _, _ = request(portal, "POST", "/setup", body=body, headers={"Origin": portal.origin})
    assert status == 403


def test_both_languages_have_every_text() -> None:
    assert set(portal_mod.STRINGS["de"]) == set(portal_mod.STRINGS["en"])
    assert portal_mod.pick_language({}, "fr-FR,de;q=0.8") == "de"
    assert portal_mod.pick_language({"lang": ["en"]}, "de") == "en"
    assert portal_mod.pick_language({}, "") == "en"

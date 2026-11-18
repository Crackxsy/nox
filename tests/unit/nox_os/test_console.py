"""The Nox-OS console status screen (os/pi-gen/stage-nox/03-hardening/files/nox_console.py): what a
screen on the Pi shows - versions, addresses, the dashboard URL, pairing, health, SSH - and nothing
secret."""

from __future__ import annotations

import json

from tests.unit.nox_os.conftest import HARDENING, load_tool

console = load_tool("nox_console", HARDENING)

NOW = 1_800_000_000.0
RELEASE = console.os_release(
    'NOX_OS_VERSION=v0.7.0\nNOX_OS_NAME="Nox-OS (based on Raspberry Pi OS)"\n'
)


def _link(name: str, up: bool, *addrs: tuple[str, str, str]) -> dict[str, object]:
    flags = ["UP"] if up else []
    info = [{"family": f, "local": a, "scope": s} for f, a, s in addrs]
    return {"ifname": name, "flags": flags, "addr_info": info}


IP_JSON = json.dumps(
    [
        _link("lo", True, ("inet", "127.0.0.1", "host")),
        _link(
            "eth0",
            True,
            ("inet", "192.0.2.10", "global"),
            ("inet6", "2001:db8::10", "global"),
            ("inet6", "fe80::1", "link"),
        ),
        _link("wlan0", False, ("inet", "192.0.2.11", "global")),
    ]
)
HEALTH = {
    "version": "0.7.0",
    "level": "normal",
    "components": {
        "lan": {"status": "available", "reason": "2 paired device(s)"},
        "voice": {"status": "limited", "reason": "model missing"},
        "db": {"status": "available", "reason": ""},
    },
}
FIREWALL = {
    "lan_enabled": True,
    "dashboard_port": 47843,
    "node_port": 47844,
    "ssh": False,
    "setup": False,
}


def test_addresses_of_interfaces_that_are_up_without_loopback_or_link_local() -> None:
    assert console.addresses(IP_JSON) == ["192.0.2.10", "2001:db8::10"]
    assert console.addresses("not json") == []


def test_the_screen_says_what_a_person_needs() -> None:
    text = console.render(RELEASE, ["192.0.2.10"], HEALTH, FIREWALL, {"on": False}, NOW)
    assert text.startswith("Nox-OS v0.7.0\n")
    assert "Nox:            0.7.0, normal" in text
    assert "limited: voice" in text
    assert "Pairing:        2 paired device(s)" in text
    assert "https://192.0.2.10:47843/dashboard/" in text
    assert "https://nox.local:47843/dashboard/" in text
    assert "Remote access:  SSH off" in text
    assert all(len(line) <= 80 for line in text.splitlines())


def test_ipv6_urls_are_bracketed_and_ssh_on_is_visible() -> None:
    ssh = {"on": True, "until": NOW + 1800}
    text = console.render(RELEASE, ["2001:db8::10"], HEALTH, FIREWALL, ssh, NOW)
    assert "https://[2001:db8::10]:47843/dashboard/" in text
    assert "Remote access:  SSH ON until" in text
    # An end in the past is off, whatever the file says.
    stale = console.render(RELEASE, [], HEALTH, FIREWALL, {"on": True, "until": NOW - 1}, NOW)
    assert "SSH off" in stale


def test_honest_when_nox_or_the_network_is_not_there() -> None:
    text = console.render({}, [], None, {"lan_enabled": False}, None, NOW)
    assert "not answering yet" in text
    assert "no network yet" in text
    assert "network access is off" in text


def test_nothing_secret_reaches_the_screen() -> None:
    health = HEALTH | {"token": "secret-token-value"}
    ssh = {"on": True, "until": NOW + 60, "keys": ["SHA256:abcdef"]}
    text = console.render(RELEASE, ["192.0.2.10"], health, FIREWALL, ssh, NOW)
    assert "secret-token-value" not in text and "SHA256" not in text


BOOT = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
SETUP_CONSOLE = {
    "ssid": "Nox-Setup-1A2B",
    "psk": "ABCD-EFGH-JKMN-PQRS",  # fictitious, the device's format
    "psk_from_card": False,
    "claim_code": "ZYXW-VTSR-QPNM",
    "qr": ["\u2588\u2580\u2588", "\u2588\u2584\u2588"],
}


def test_the_setup_block_shows_hotspot_qr_and_claim_code_only_while_the_window_is_open() -> None:
    mode = {"v": 1, "boot_id": BOOT, "until": 1800}
    view = console.setup_view(mode, SETUP_CONSOLE, {}, BOOT, 600.0)
    assert view is not None
    screen = console.render(
        RELEASE,
        ["192.0.2.10"],
        HEALTH,
        {"dashboard_port": 47843},
        None,
        NOW,
        setup=view,
        claimed=False,
    )
    assert "Setup Wi-Fi:    Nox-Setup-1A2B   password ABCD-EFGH-JKMN-PQRS" in screen
    assert "  \u2588\u2580\u2588" in screen and "ZYXW-VTSR-QPNM" in screen
    assert "Owner:          not claimed yet" in screen
    # Over, or another boot's: nothing of it.
    assert console.setup_view(mode, SETUP_CONSOLE, {}, BOOT, 1800.0) is None
    assert console.setup_view({**mode, "boot_id": "x"}, SETUP_CONSOLE, {}, BOOT, 600.0) is None
    plain = console.render(RELEASE, ["192.0.2.10"], HEALTH, None, None, NOW, setup=None)
    assert "ABCD-EFGH" not in plain and "\u2588" not in plain

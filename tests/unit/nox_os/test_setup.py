"""Nox-OS first boot and setup mode (os/pi-gen/stage-nox/04-firstboot/files/nox_setup.py): the
provisioning file is checked strictly, imported and securely deleted - its secrets never logged;
the setup window opens only while unclaimed, at most 30 minutes after power-on, and closes with
every setup secret; the setup hotspot comes up only when it is needed; the portal's requests are
checked again by root. Only fictitious data (Springfield-WLAN, example.invalid)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from nox.noxos.claim import normalize_claim_code as core_normalize
from tests.unit.nox_os.conftest import FIRSTBOOT, HARDENING, REPO, load_tool

setup = load_tool("nox_setup", FIRSTBOOT)
helper = load_tool("nox_os_helper", HARDENING)

SCHEMA = REPO / "os" / "provisioning" / "nox-provision.schema.json"
EXAMPLE = REPO / "os" / "provisioning" / "nox-provision.example.json"
OWN_UID = getattr(os, "getuid", lambda: 0)()
BOOT_ID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
PSK = "springfield-example-psk"  # fictitious
SSID = "Springfield-WLAN"  # fictitious


class FakeRun:
    """systemctl, nmcli, hostname and rfkill as nox_setup.py calls them."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.devices = "eth0:ethernet:unavailable:\nwlan0:wifi:disconnected:\n"
        self.scan = f"{SSID}:72:WPA2\nOther\\:Net:40:\n{SSID}:30:WPA2\n:90:WPA2\n"
        self.up_ok = {"nox-setup-hotspot": True, "nox-wifi": True}
        self.up_error = ""

    def __call__(self, *args: str, timeout: float = 0) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        out, code, err = "", 0, ""
        if args[:1] == ("nmcli",):
            if "status" in args:
                out = self.devices
            elif "list" in args:
                out = self.scan
            elif "up" in args:
                ok = self.up_ok.get(args[-1], False)
                code, err = (0, "") if ok else (4, self.up_error)
        return subprocess.CompletedProcess(args, code, out, err)

    def ran(self, *prefix: str) -> bool:
        return any(call[: len(prefix)] == prefix for call in self.calls)


@pytest.fixture
def run() -> FakeRun:
    return FakeRun()


@pytest.fixture
def clock() -> dict[str, float]:
    return {"uptime": 30.0}


@pytest.fixture
def system(tmp_path: Path, run: FakeRun, clock: dict[str, float]) -> Any:
    ident = tmp_path / "proc/sys/kernel/random/boot_id"
    ident.parent.mkdir(parents=True)
    ident.write_text(BOOT_ID + "\n", "ascii")
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/machine-id").write_text("0123456789abcdef0123456789abcdef\n", "ascii")
    (tmp_path / "boot/firmware").mkdir(parents=True)
    return setup.System(
        run=run,
        uptime=lambda: clock["uptime"],
        clock=lambda: 1_800_000_000.0,
        sleep=lambda _s: None,
        root=tmp_path,
        gid_of=lambda _n: 0,
        uid_of=lambda _n: OWN_UID,
        root_uid=OWN_UID,
        chown=False,
    )


def provision(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "boot/firmware/nox-provision.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data), "utf-8")
    return path


def read(tmp_path: Path, path: str) -> str:
    return (tmp_path / path.lstrip("/")).read_text("utf-8")


def mode_of(path: Path) -> int:
    return path.stat().st_mode & 0o777


#: The hotspot password the device makes: 16 characters in four groups, the dashes part of it.
PSK_PATTERN = r"[0-9A-HJKMNP-TV-Z]{4}(-[0-9A-HJKMNP-TV-Z]{4}){3}"

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="file modes and links")

# ---- the provisioning file -----------------------------------------------------------------


def test_the_example_is_valid_and_matches_the_schema() -> None:
    parsed = setup.validate(EXAMPLE.read_bytes())
    assert parsed.wifi is not None and parsed.wifi.ssid == SSID
    assert parsed.claim_hash == setup.claim_hash("ABCDEFGHJKMN")
    schema = json.loads(SCHEMA.read_text("utf-8"))
    assert set(schema["properties"]) == set(setup._TOP)  # noqa: SLF001
    wifi = next(o for o in schema["properties"]["wifi"]["oneOf"] if o.get("type") == "object")
    assert set(wifi["properties"]) == set(setup._WIFI)  # noqa: SLF001
    assert [x for x in schema["properties"]["locale"]["enum"] if x] == list(setup.LOCALES)
    hostname = schema["properties"]["hostname"]["pattern"]
    for name in ("nox", "nox-2", "a" * 63, "123", "-nox", "Nox", "a" * 64):
        assert bool(re.fullmatch(hostname, name)) is setup.hostname_ok(name), name


def test_secrets_never_reach_a_repr() -> None:
    parsed = setup.validate(EXAMPLE.read_bytes())
    assert "example-wifi-password-not-real" not in repr(parsed)
    assert "example-setup-password" not in repr(parsed)


@pytest.mark.parametrize(
    ("data", "code"),
    [
        ("{", "not_json"),
        ("[]", "not_an_object"),
        ('{"v": 1, "v": 1}', "duplicate_field"),
        ('{"v": 1, "hostname": NaN}', "not_json"),
        ({"v": 2}, "unsupported_version"),
        ({"v": True}, "unsupported_version"),
        ({}, "unsupported_version"),
        ({"v": 1, "ssid": SSID}, "unknown_field"),
        ({"v": 1, "hostname": "Nox_Pi"}, "invalid_hostname"),
        ({"v": 1, "hostname": "1234"}, "invalid_hostname"),
        ({"v": 1, "timezone": "../../etc/shadow"}, "invalid_timezone"),
        ({"v": 1, "timezone": "Europe/../../x"}, "invalid_timezone"),
        ({"v": 1, "locale": "fr_FR"}, "invalid_locale"),
        ({"v": 1, "country": "de"}, "invalid_country"),
        ({"v": 1, "wifi": {"ssid": SSID, "psk": PSK}}, "country_required"),
        ({"v": 1, "country": "DE", "wifi": {"ssid": "", "psk": PSK}}, "invalid_wifi_ssid"),
        ({"v": 1, "country": "DE", "wifi": {"ssid": "x" * 33}}, "invalid_wifi_ssid"),
        ({"v": 1, "country": "DE", "wifi": {"ssid": "a\nb"}}, "invalid_wifi_ssid"),
        ({"v": 1, "country": "DE", "wifi": {"ssid": SSID, "psk": "short"}}, "invalid_wifi_psk"),
        ({"v": 1, "country": "DE", "wifi": {"ssid": SSID, "psk": "x" * 64}}, "invalid_wifi_psk"),
        ({"v": 1, "country": "DE", "wifi": {"ssid": SSID, "hidden": "yes"}}, "invalid_wifi_hidden"),
        ({"v": 1, "country": "DE", "wifi": {"ssid": SSID, "key": PSK}}, "invalid_wifi"),
        ({"v": 1, "claim_hash": "md5:abc"}, "invalid_claim_hash"),
        ({"v": 1, "hotspot_psk": "has space"}, "invalid_hotspot_psk"),
        ({"v": 1, "hotspot_psk": "short"}, "invalid_hotspot_psk"),
        ({"v": 1, "hotspot_psk": "fifteen-chars-x"}, "invalid_hotspot_psk"),  # 16 at least
        ({"v": 1, "reclaim": "yes"}, "invalid_reclaim"),
    ],
)
def test_the_provisioning_file_is_refused_field_by_field(data: object, code: str) -> None:
    raw = (data if isinstance(data, str) else json.dumps(data)).encode()
    with pytest.raises(setup.ProvisionError) as refused:
        setup.validate(raw)
    assert refused.value.code == code
    assert PSK not in str(refused.value)


def test_what_the_provisioning_file_may_be() -> None:
    raw = "﻿" + json.dumps(
        {
            "v": 1,
            "country": "DE",
            "wifi": {"ssid": "Café Springfield", "psk": "a" * 64, "hidden": True},
            "hostname": None,
        }
    )
    parsed = setup.validate(raw.encode())
    assert parsed.wifi is not None and parsed.wifi.hidden is True and parsed.hostname is None
    assert setup.validate(b'{"v": 1, "wifi": null}').wifi is None
    open_net = setup.validate(b'{"v": 1, "country": "DE", "wifi": {"ssid": "x", "psk": null}}')
    assert open_net.wifi is not None and open_net.wifi.psk is None
    with pytest.raises(setup.ProvisionError):
        setup.validate(b"x" * (setup.MAX_PROVISION_BYTES + 1))


# ---- import and secure delete --------------------------------------------------------------


def test_import_writes_the_keyfile_and_securely_deletes_the_file(
    tmp_path: Path, system: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = json.loads(EXAMPLE.read_text("utf-8"))
    data.pop("timezone")  # needs a zoneinfo tree (its own test)
    data["wifi"]["psk"] = "pass with\\back slash"
    path = provision(tmp_path, data)
    original = path.read_bytes()
    seen: list[bytes] = []
    truncate = os.ftruncate

    def observe(fd: int, length: int) -> None:
        with open(path, "rb") as handle:  # what is on the card right before it is emptied
            seen.append(handle.read())
        truncate(fd, length)

    monkeypatch.setattr(setup.os, "ftruncate", observe)
    assert setup.boot(system) == 0
    assert not path.exists()
    assert len(seen) == 1 and len(seen[0]) == len(original) and seen[0] != original
    keyfile = tmp_path / "etc/NetworkManager/system-connections/nox-wifi.nmconnection"
    text = keyfile.read_text("utf-8")
    assert "ssid=" + "".join(f"{b};" for b in SSID.encode()) in text
    assert "psk=pass\\swith\\\\back\\sslash" in text
    claim = json.loads(read(tmp_path, "var/lib/nox-os/claim.json"))
    assert claim == {"v": 1, "hash": data["claim_hash"], "source": "card"}
    assert read(tmp_path, "var/lib/nox-os/hotspot-psk").strip() == "example-setup-password"
    assert "ieee80211_regdom=DE" in read(tmp_path, "etc/modprobe.d/nox-os-wifi-country.conf")
    assert read(tmp_path, "etc/hostname") == "nox\n"
    assert read(tmp_path, "etc/default/locale") == "LANG=de_DE.UTF-8\n"
    status = json.loads(read(tmp_path, "run/nox-os/setup/status.json"))
    assert status["provision"]["imported"] is True and status["provision"]["deleted"] is True
    logged = capsys.readouterr().err
    assert "imported" in logged
    for secret in ("pass with", SSID, "example-setup-password", data["claim_hash"]):
        assert secret not in logged and secret not in json.dumps(status)
    if sys.platform != "win32":
        assert mode_of(keyfile) == 0o600
        assert mode_of(tmp_path / "var/lib/nox-os/claim.json") == 0o600
    # The card's code: no code made on the device, its hotspot password from the card.
    assert not (tmp_path / "run/nox-os/setup/claim-code").exists()
    console = json.loads(read(tmp_path, "run/nox-os/setup/console.json"))
    assert console["psk"] == "example-setup-password" and console["claim_code"] is None
    assert not (tmp_path / "run/nox-os/setup/blink").exists()


def test_a_refused_file_is_deleted_too_and_the_reason_kept(tmp_path: Path, system: Any) -> None:
    path = provision(tmp_path, {"v": 1, "country": "DE", "wifi": {"ssid": SSID, "psk": "short"}})
    setup.boot(system)
    assert not path.exists()
    status = json.loads(read(tmp_path, "run/nox-os/setup/status.json"))
    assert status["provision"] == {"imported": False, "error": "invalid_wifi_psk", "deleted": True}
    assert not (tmp_path / "etc/NetworkManager/system-connections/nox-wifi.nmconnection").exists()


@POSIX
def test_the_time_zone_must_exist_in_the_image(tmp_path: Path, system: Any) -> None:
    zone = tmp_path / "usr/share/zoneinfo/Europe/Berlin"
    zone.parent.mkdir(parents=True)
    zone.write_bytes(b"TZif")
    provision(tmp_path, {"v": 1, "timezone": "Europe/Berlin"})
    setup.boot(system)
    assert os.readlink(tmp_path / "etc/localtime") == "/usr/share/zoneinfo/Europe/Berlin"
    provision(tmp_path, {"v": 1, "timezone": "Mars/Olympus"})
    setup.boot(system)
    status = json.loads(read(tmp_path, "run/nox-os/setup/status.json"))
    assert status["provision"]["warning"] == "unknown_timezone"


def test_a_claimed_device_ignores_a_cards_claim_code(tmp_path: Path, system: Any) -> None:
    (tmp_path / "var/lib/nox-os-claim").mkdir(parents=True)
    (tmp_path / "var/lib/nox-os-claim/claimed").write_text("{}", "ascii")
    provision(tmp_path, {"v": 1, "claim_hash": setup.claim_hash("ABCDEFGHJKMN")})
    setup.boot(system)
    assert not (tmp_path / "var/lib/nox-os/claim.json").exists()
    assert not (tmp_path / "run/nox-os/setup-mode").exists()


# ---- the window ----------------------------------------------------------------------------


def test_an_unclaimed_device_opens_the_window_with_this_boots_secrets(
    tmp_path: Path, system: Any
) -> None:
    setup.boot(system)
    mode = json.loads(read(tmp_path, "run/nox-os/setup-mode"))
    assert mode == {"v": 1, "boot_id": BOOT_ID, "until": 1800, "reason": "unclaimed"}
    assert setup.setup_mode(system) is not None
    shown = read(tmp_path, "run/nox-os/setup/claim-code").strip()
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}", shown)
    claim = json.loads(read(tmp_path, "var/lib/nox-os/claim.json"))
    code = setup.normalize_claim_code(shown)
    assert claim == {"v": 1, "hash": setup.claim_hash(code), "source": "device"}
    console = json.loads(read(tmp_path, "run/nox-os/setup/console.json"))
    assert re.fullmatch(PSK_PATTERN, console["psk"]) and console["claim_code"] == shown
    assert re.fullmatch(r"Nox-Setup-[0-9A-F]{4}", console["ssid"])
    assert not (tmp_path / "run/nox-os/setup/blink").exists()  # 40 bits: no hotspot password
    if sys.platform != "win32":
        assert mode_of(tmp_path / "run/nox-os/setup/claim-code") == 0o640
    # The next boot: new secrets.
    setup.close(system, "test")
    setup.boot(system)
    assert read(tmp_path, "run/nox-os/setup/claim-code").strip() != shown


def test_the_window_is_this_boots_and_ends(
    tmp_path: Path, system: Any, clock: dict[str, float]
) -> None:
    setup.boot(system)
    clock["uptime"] = 1799.0
    assert setup.setup_mode(system) is not None
    clock["uptime"] = 1800.0
    assert setup.setup_mode(system) is None  # its end: closed, even before the file is gone
    clock["uptime"] = 30.0
    (tmp_path / "proc/sys/kernel/random/boot_id").write_text("1" * 8 + BOOT_ID[8:], "ascii")
    assert setup.setup_mode(system) is None  # another boot's file
    # The same rule in the root helper and the firewall.
    hsys = helper.System(root=tmp_path, root_uid=OWN_UID, uptime=lambda: 30.0)
    assert helper.in_setup_mode(hsys) is False
    (tmp_path / "proc/sys/kernel/random/boot_id").write_text(BOOT_ID, "ascii")
    assert helper.in_setup_mode(hsys) is True


def test_an_open_window_is_never_extended(
    tmp_path: Path, system: Any, clock: dict[str, float]
) -> None:
    setup.boot(system)
    clock["uptime"] = 600.0
    assert setup.open_window(system, "unclaimed")["until"] == 1800


def test_only_a_test_image_shortens_the_window(tmp_path: Path, system: Any) -> None:
    override = tmp_path / "etc/nox-os/testing/setup-window-s"
    override.parent.mkdir(parents=True)
    override.write_text("240\n", "ascii")
    assert setup.window_seconds(system) == 240
    override.write_text("5\n", "ascii")
    assert setup.window_seconds(system) == setup.MIN_TEST_WINDOW_S
    override.write_text("99999\n", "ascii")
    assert setup.window_seconds(system) == setup.WINDOW_S  # never longer


def test_a_claimed_device_has_no_window_unless_root_asked_for_a_reset(
    tmp_path: Path, system: Any
) -> None:
    (tmp_path / "var/lib/nox-os-claim").mkdir(parents=True)
    (tmp_path / "var/lib/nox-os-claim/claimed").write_text("{}", "ascii")
    setup.boot(system)
    assert setup.setup_mode(system) is None
    assert not (tmp_path / "run/nox-os/setup/console.json").exists()
    (tmp_path / "var/lib/nox-os").mkdir(parents=True, exist_ok=True)
    (tmp_path / "var/lib/nox-os/reset-maintenance").write_text("", "ascii")
    setup.boot(system)
    assert setup.setup_mode(system)["reason"] == "reset-maintenance"
    assert not (tmp_path / "run/nox-os/setup/claim-code").exists()  # no claim, no code


def test_closing_takes_every_setup_secret_with_it(
    tmp_path: Path, system: Any, run: FakeRun, clock: dict[str, float]
) -> None:
    setup.boot(system)
    (tmp_path / "run/nox-os/setup/pending.json").write_text("{}", "ascii")
    clock["uptime"] = 1801.0
    setup.tick(system)
    for gone in (
        "run/nox-os/setup-mode",
        "run/nox-os/setup/claim-code",
        "run/nox-os/setup/console.json",
        "run/nox-os/setup/blink",
        "run/nox-os/setup/pending.json",
        "var/lib/nox-os/claim.json",
        "run/NetworkManager/system-connections/nox-setup-hotspot.nmconnection",
    ):
        assert not (tmp_path / gone).exists(), gone
    assert run.ran("systemctl", "restart", "nox-firewall.service")
    assert run.ran("systemctl", "stop", "nox-setup-portal.service")
    assert run.ran("systemctl", "stop", "--no-block", "nox-setup.timer")
    assert json.loads(read(tmp_path, "run/nox-os/setup/status.json"))["state"] == "closed"


# ---- the hotspot ---------------------------------------------------------------------------


def _hotspot_up(run: FakeRun) -> bool:
    return run.ran("nmcli", "--wait", "30", "connection", "up", "nox-setup-hotspot")


def test_with_a_device_code_the_hotspot_comes_up_at_once(
    tmp_path: Path, system: Any, run: FakeRun
) -> None:
    setup.boot(system)
    setup.tick(system)
    assert _hotspot_up(run)
    keyfile = tmp_path / "run/NetworkManager/system-connections/nox-setup-hotspot.nmconnection"
    text = keyfile.read_text("utf-8")
    console = json.loads(read(tmp_path, "run/nox-os/setup/console.json"))
    for line in (
        "mode=ap",
        "interface-name=wlan0",
        "key-mgmt=wpa-psk",
        "proto=rsn",
        "pairwise=ccmp",
        f"psk={console['psk']}",
        "method=shared",
        "address1=10.42.0.1/24",
        "autoconnect=false",
    ):
        assert line in text.splitlines(), line
    assert "ssid=" + "".join(f"{b};" for b in console["ssid"].encode()) in text
    assert json.loads(read(tmp_path, "run/nox-os/setup/hotspot.json"))["iface"] == "wlan0"
    networks = json.loads(read(tmp_path, "run/nox-os/setup/networks.json"))["networks"]
    assert networks == [
        {"ssid": SSID, "signal": 72, "open": False},
        {"ssid": "Other:Net", "signal": 40, "open": True},
    ]
    assert run.ran("systemctl", "start", "nox-setup-portal.service")
    # The scan happened before the hotspot took the radio.
    order = [c for c in run.calls if c[:1] == ("nmcli",)]
    assert order.index(next(c for c in order if "list" in c)) < order.index(
        next(c for c in order if "up" in c)
    )
    if sys.platform != "win32":
        assert mode_of(keyfile) == 0o600


def test_with_a_cards_code_the_hotspot_waits_for_the_network(
    tmp_path: Path, system: Any, run: FakeRun, clock: dict[str, float]
) -> None:
    provision(tmp_path, {"v": 1, "claim_hash": setup.claim_hash("ABCDEFGHJKMN")})
    setup.boot(system)
    setup.tick(system)
    assert not _hotspot_up(run)  # 30 s after boot: Ethernet or the Wi-Fi may still come
    run.devices = "eth0:ethernet:connected:Wired connection 1\nwlan0:wifi:disconnected:\n"
    clock["uptime"] = 200.0
    setup.tick(system)
    assert not _hotspot_up(run)  # online: no hotspot
    run.devices = "eth0:ethernet:unavailable:\nwlan0:wifi:disconnected:\n"
    setup.tick(system)
    assert _hotspot_up(run)


def test_the_hotspot_never_takes_the_radio_from_the_home_network(
    tmp_path: Path, system: Any, run: FakeRun
) -> None:
    setup.boot(system)
    run.devices = "wlan0:wifi:connected:nox-wifi\n"
    setup.tick(system)
    assert not _hotspot_up(run)


def test_without_a_wifi_device_the_profile_is_prepared_and_the_reason_kept(
    tmp_path: Path, system: Any, run: FakeRun
) -> None:
    setup.boot(system)
    run.devices = "eth0:ethernet:unavailable:\n"
    setup.tick(system)
    assert not _hotspot_up(run)
    assert (
        tmp_path / "run/NetworkManager/system-connections/nox-setup-hotspot.nmconnection"
    ).exists()
    assert json.loads(read(tmp_path, "run/nox-os/setup/status.json"))["hotspot"] == "no_wifi_device"
    run.calls.clear()
    setup.tick(system)
    assert not run.ran("nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY")  # not tried every 15 s


# ---- the portal's requests -----------------------------------------------------------------


def _request(tmp_path: Path, payload: dict[str, Any]) -> None:
    folder = tmp_path / "run/nox-os/setup/portal"
    folder.mkdir(parents=True, exist_ok=True)
    full = {"claim_code": "ABCD-EFGH-JKMN", "peer": "10.42.0.20", **payload}
    (folder / "request.json").write_text(json.dumps(full), "utf-8")


class AskHelper:
    """The root helper's `stage`: refuses a wrong claim code, otherwise keeps PIN and passphrase."""

    def __init__(self, answer: dict[str, Any] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.answer = answer or {"ok": True, "pending_pin": True, "pending_passphrase": True}

    def __call__(self, _system: Any, lines: list[str]) -> dict[str, Any]:
        self.calls.append(lines)
        return self.answer


WRONG_CODE = {"ok": False, "code": "claim_wrong"}


def test_apply_stages_the_secrets_with_the_code_then_joins_the_home_wifi(
    tmp_path: Path, system: Any, run: FakeRun
) -> None:
    setup.boot(system)
    _request(
        tmp_path,
        {"v": 1, "wifi": {"ssid": SSID, "psk": PSK}, "ticket": "t" * 32},
    )
    ask = AskHelper()
    setup.apply(system, helper=ask)
    b64 = setup._b64  # noqa: SLF001
    assert ask.calls == [
        ["stage 10.42.0.20", "code " + b64("ABCDEFGHJKMN"), "ticket " + b64("t" * 32)]
    ]
    # Nothing applied by the portal itself: the helper keeps them for the claim.
    assert not (tmp_path / "run/nox-os/setup/pending-pin").exists()
    assert not (tmp_path / "run/nox-os/setup/portal/request.json").exists()
    assert run.ran("nmcli", "--wait", "60", "connection", "up", "nox-wifi")
    status = json.loads(read(tmp_path, "run/nox-os/setup/status.json"))
    assert status["state"] == "joined" and status["code_ok"] is True
    assert PSK not in json.dumps(status) and "135790" not in json.dumps(status)
    keyfile = read(tmp_path, "etc/NetworkManager/system-connections/nox-wifi.nmconnection")
    assert f"psk={PSK}" in keyfile


def test_a_wrong_claim_code_applies_nothing_at_all(
    tmp_path: Path, system: Any, run: FakeRun
) -> None:
    setup.boot(system)
    _request(
        tmp_path,
        {"v": 1, "wifi": {"ssid": SSID, "psk": PSK}},
    )
    setup.apply(system, helper=AskHelper(WRONG_CODE))
    assert json.loads(read(tmp_path, "run/nox-os/setup/status.json"))["error"] == "claim_wrong"
    assert not run.ran("nmcli", "--wait", "60", "connection", "up", "nox-wifi")
    assert not (tmp_path / "etc/NetworkManager/system-connections/nox-wifi.nmconnection").exists()


def test_a_request_without_a_claim_code_is_refused_by_root(tmp_path: Path, system: Any) -> None:
    setup.boot(system)
    _request(tmp_path, {"v": 1, "claim_code": ""})
    ask = AskHelper()
    setup.apply(system, helper=ask)
    assert ask.calls == []
    assert json.loads(read(tmp_path, "run/nox-os/setup/status.json"))["error"] == "claim_wrong"


def test_a_wrong_wifi_password_brings_the_hotspot_back_with_the_reason(
    tmp_path: Path, system: Any, run: FakeRun
) -> None:
    setup.boot(system)
    run.up_ok["nox-wifi"] = False
    run.up_error = "Error: Connection activation failed: Secrets were required, but not provided."
    _request(tmp_path, {"v": 1, "wifi": {"ssid": SSID, "psk": PSK}})
    setup.apply(system, helper=AskHelper())
    status = json.loads(read(tmp_path, "run/nox-os/setup/status.json"))
    assert (status["state"], status["error"], status["tried_ssid"]) == (
        "failed",
        "wrong_password",
        SSID,
    )
    assert _hotspot_up(run)


def test_root_checks_the_request_again(tmp_path: Path, system: Any, run: FakeRun) -> None:
    setup.boot(system)
    ask = AskHelper()
    _request(tmp_path, {"v": 1, "wifi": {"ssid": SSID, "psk": "short"}})
    setup.apply(system, helper=ask)
    assert json.loads(read(tmp_path, "run/nox-os/setup/status.json"))["error"] == "invalid_wifi_psk"
    # No PIN or passphrase over the hotspot's plain HTTP, whatever a request carries.
    for field in ("pin", "passphrase"):
        _request(tmp_path, {"v": 1, field: "135790135790"})
        setup.apply(system, helper=ask)
        status = json.loads(read(tmp_path, "run/nox-os/setup/status.json"))
        assert status["error"] == "unknown_field"
    _request(tmp_path, {"v": 1, "peer": "not an address"})
    setup.apply(system, helper=ask)
    assert json.loads(read(tmp_path, "run/nox-os/setup/status.json"))["error"] == "invalid_peer"
    # A request someone else wrote counts for nothing.
    system.uid_of = lambda _n: OWN_UID + 1
    _request(tmp_path, {"v": 1})
    setup.apply(system, helper=ask)
    assert not (tmp_path / "run/nox-os/setup/portal/request.json").exists()
    assert ask.calls == []


def test_no_request_counts_once_the_window_closed(
    tmp_path: Path, system: Any, clock: dict[str, float]
) -> None:
    setup.boot(system)
    clock["uptime"] = 1900.0
    _request(tmp_path, {"v": 1})
    ask = AskHelper()
    setup.apply(system, helper=ask)
    assert ask.calls == []


# ---- small pieces --------------------------------------------------------------------------


def test_claim_codes_are_the_same_on_every_side() -> None:
    for _ in range(20):
        code = setup.new_claim_code()
        shown = setup.show_claim_code(code)
        assert setup.normalize_claim_code(shown) == code == core_normalize(shown)
        assert helper.normalize_claim_code(shown.lower()) == code
        assert helper.claim_hash(code) == setup.claim_hash(code)
    assert setup.normalize_claim_code("abcd-efgh-jkmn") == "ABCDEFGHJKMN"


def test_the_hotspot_name_is_per_device_and_stable() -> None:
    one = setup.hotspot_ssid("0123456789abcdef0123456789abcdef")
    assert one == setup.hotspot_ssid("0123456789abcdef0123456789abcdef\n")
    assert one != setup.hotspot_ssid("fedcba9876543210fedcba9876543210")
    assert "0123" not in one.removeprefix("Nox-Setup-") or len(one) == 14


def test_hotspot_passwords_have_80_bits() -> None:
    seen = {setup.new_hotspot_psk() for _ in range(50)}
    assert len(seen) == 50 and all(re.fullmatch(PSK_PATTERN, p) for p in seen)
    # 16 characters of a 32-letter alphabet.
    assert 16 * 5 == 80 and len(setup.CLAIM_ALPHABET) == 32
    # The blink digits (O5) are not the hotspot password.
    assert re.fullmatch(r"[0-9]{12}", setup.new_blink_digits())


def test_the_wifi_qr_code_payload_is_escaped() -> None:
    assert setup.wifi_qr_payload("Nox-Setup-1A2B", "ABCD-EFGH-JKMN-PQRS") == (
        "WIFI:T:WPA;S:Nox-Setup-1A2B;P:ABCD-EFGH-JKMN-PQRS;;"
    )
    assert (
        setup.wifi_qr_payload('a;b,c:d"e\\f', "p;w")
        == 'WIFI:T:WPA;S:a\\;b\\,c\\:d\\"e\\\\f;P:p\\;w;;'
    )


def test_the_qr_code_is_drawn_for_a_dark_console() -> None:
    # A 2x2 "matrix": dark top-left and bottom-right.
    lines = setup.qr_text([[1, 0], [0, 1]], quiet=1)
    # A lit upper half = a light module above a dark one; dark modules stay unlit (black).
    assert lines == ["\u2588\u2580\u2588\u2588", "\u2588\u2588\u2584\u2588"]
    assert set("".join(setup.qr_text([[0]], quiet=2))) == {"\u2588"}  # all light: full blocks


def test_the_console_gets_the_qr_code_from_noxs_python(tmp_path: Path, system: Any) -> None:
    python = tmp_path / "opt/nox/current/.venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("", "ascii")
    seen: list[tuple[list[str], str]] = []

    def run_input(args: list[str], data: str) -> str:
        seen.append((args, data))
        return json.dumps([[1, 0], [0, 1]])

    system.run_input = run_input
    setup.boot(system)
    console = json.loads(read(tmp_path, "run/nox-os/setup/console.json"))
    assert console["qr"] and all(isinstance(line, str) for line in console["qr"])
    args, data = seen[0]
    assert console["psk"] not in " ".join(args)  # the password only on stdin
    assert data == setup.wifi_qr_payload(console["ssid"], console["psk"])


def _ca_pem() -> tuple[str, str]:
    """A throwaway self-signed certificate (fictitious name) and its fingerprint as Nox shows it."""
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    from nox.lan.tls import fingerprint

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Nox test CA (example.invalid)")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    return pem, fingerprint(cert.public_bytes(serialization.Encoding.DER))


def test_root_publishes_the_ca_fingerprint_from_noxs_file(tmp_path: Path, system: Any) -> None:
    setup.boot(system)
    assert not (tmp_path / "run/nox-os/setup/ca_fingerprint").exists()  # no CA yet
    pem, expected = _ca_pem()
    ca = tmp_path / "var/lib/nox/tls/ca.crt"
    ca.parent.mkdir(parents=True)
    ca.write_text(pem, "ascii")
    setup.tick(system)
    assert read(tmp_path, "run/nox-os/setup/ca_fingerprint").strip() == expected
    if sys.platform != "win32":
        assert mode_of(tmp_path / "run/nox-os/setup/ca_fingerprint") == 0o640
    setup.close(system, "test")
    assert not (tmp_path / "run/nox-os/setup/ca_fingerprint").exists()


def _claimed(tmp_path: Path) -> None:
    (tmp_path / "var/lib/nox-os-claim").mkdir(parents=True, exist_ok=True)
    (tmp_path / "var/lib/nox-os-claim/claimed").write_text("{}", "ascii")
    (tmp_path / "var/lib/nox-os").mkdir(parents=True, exist_ok=True)
    (tmp_path / "var/lib/nox-os/ssh_keys").write_text("ssh-ed25519 AAAA\n", "ascii")


@pytest.mark.parametrize("how", ["file", "provisioning"])
def test_a_reclaim_marker_forgets_the_owner_and_opens_setup(
    tmp_path: Path, system: Any, how: str
) -> None:
    _claimed(tmp_path)
    marker = tmp_path / "boot/firmware/nox-reclaim"
    if how == "file":
        marker.write_text("reclaim\n", "ascii")
    else:
        provision(tmp_path, {"v": 1, "reclaim": True})
    setup.boot(system)
    assert not marker.exists() and not (tmp_path / "boot/firmware/nox-provision.json").exists()
    assert not (tmp_path / "var/lib/nox-os-claim/claimed").exists()
    assert not (tmp_path / "var/lib/nox-os/ssh_keys").exists()
    assert (tmp_path / "var/lib/nox-os/reset-maintenance").exists()
    note = json.loads(read(tmp_path, "var/lib/nox-os-claim/reclaim"))
    assert re.fullmatch(r"[0-9a-f]{32}", note["id"])
    # A fresh setup window, as on a new device.
    assert setup.setup_mode(system)["reason"] == "unclaimed"
    assert (tmp_path / "run/nox-os/setup/claim-code").exists()


def test_without_a_marker_a_claimed_device_stays_claimed(tmp_path: Path, system: Any) -> None:
    _claimed(tmp_path)
    setup.boot(system)
    assert (tmp_path / "var/lib/nox-os-claim/claimed").exists()
    assert not (tmp_path / "var/lib/nox-os-claim/reclaim").exists()


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        (
            "Error: Connection activation failed: Secrets were required, but not provided.",
            "wrong_password",
        ),
        ("Error: No network with SSID 'Springfield-WLAN' found.", "not_found"),
        ("Error: Timeout expired (60 seconds)", "wifi_failed"),
    ],
)
def test_join_errors(stderr: str, code: str) -> None:
    assert setup.join_error(stderr) == code


def test_nmcli_fields_undo_the_escapes() -> None:
    assert setup.nmcli_fields(r"a\:b:c\\d:") == ["a:b", "c\\d", ""]


def test_nothing_is_decided_while_networkmanager_cannot_answer(
    tmp_path: Path, system: Any, run: FakeRun
) -> None:
    setup.boot(system)
    original = run.__call__

    def failing(*args: str, timeout: float = 0) -> subprocess.CompletedProcess[str]:
        if args[:1] == ("nmcli",):
            run.calls.append(args)
            return subprocess.CompletedProcess(args, 8, "", "NetworkManager is not running")
        return original(*args, timeout=timeout)

    system.run = failing
    setup.tick(system)
    assert not (tmp_path / "run/NetworkManager/system-connections").exists()
    assert json.loads(read(tmp_path, "run/nox-os/setup/status.json"))["hotspot"] == "down"


def test_the_qr_matrix_program_runs_with_noxs_python() -> None:
    """The very program nox_setup.py hands Nox's Python (segno, already a dependency)."""
    payload = setup.wifi_qr_payload("Nox-Setup-1A2B", "ABCD-EFGH-JKMN-PQRS")
    done = subprocess.run(
        [sys.executable, "-I", "-c", setup._QR_MATRIX],  # noqa: SLF001
        input=payload,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    matrix = json.loads(done.stdout)
    assert len(matrix) >= 25 and all(len(row) == len(matrix) for row in matrix)
    lines = setup.qr_text(matrix)
    assert len(lines) == (len(matrix) + 4 + 1) // 2 and len(lines[0]) == len(matrix) + 4

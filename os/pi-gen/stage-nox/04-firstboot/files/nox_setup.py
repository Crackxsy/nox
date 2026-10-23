"""Nox-OS first boot and setup mode: provisioning file, setup window, setup hotspot (os/README.md).

Run by systemd as root, one short step at a time - never a daemon:

    python3 -I -S nox_setup.py boot     every boot, before the network (nox-firstboot.service):
                                         import /boot/firmware/nox-provision.json if there is one,
                                         then - while the device is unclaimed - open the setup
                                         window and make this boot's setup secrets
    python3 -I -S nox_setup.py tick     every 15 s while the window is open (nox-setup.timer):
                                         close it when its time is up, start the setup hotspot
                                         when Nox has no network (or the claim code can only be
                                         learnt there), keep the portal running while it is up
    python3 -I -S nox_setup.py apply    what the captive portal asked for (nox-setup-apply.path),
                                         only with the right claim code: PIN and maintenance
                                         passphrase kept for the claim (the root helper's
                                         `stage`), then the home Wi-Fi
    python3 -I -S nox_setup.py close [reason]   end setup mode now (the root helper after a claim)
    python3 -I -S nox_setup.py status   what is going on, without any secret

**The setup window.** Root writes `/run/nox-os/setup-mode` (JSON: this boot's id and the end, in
seconds since boot - CLOCK_BOOTTIME, which no clock change moves; a Pi has no real-time clock). It
is open at most 30 minutes after power-on and only while the device is unclaimed (or root asked
for a maintenance-passphrase reset). Nothing reopens it but another boot: a power cycle is the
physical act that proves someone is at the device. Everything that grants something during setup
- the setup hotspot and its ports, the portal, `maint set`, the claim - checks the file itself,
so an expired window grants nothing even before `tick` removed it.

**Secrets of the setup**, all in RAM under /run, all gone when the window closes:
  * the hotspot password: from the provisioning file (printed on the setup card), else 12 random
    digits made at every boot - shown on a screen plugged into the Pi and, from O5 on, blinked by
    its LED (`/run/nox-os/setup/blink`, root only);
  * the claim code: only its hash on the device when it came on the setup card; otherwise 12
    random characters made at every boot, shown on the screen and in the portal;
  * the security PIN and maintenance passphrase typed into the portal - kept by the root helper
    only after the right claim code, and applied only by the claim itself.

The provisioning file is checked strictly (`validate`), imported, then overwritten and deleted.
Its Wi-Fi password goes into a NetworkManager keyfile (root, 0600) and nowhere else; nothing of
its contents is ever logged.

Standard library only (`python3 -I -S`).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Never through a link; bytes as they are (O_BINARY: the tests on Windows).
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)

# ---- where things live ---------------------------------------------------------------------

BOOT = Path("/boot/firmware")
PROVISION = BOOT / "nox-provision.json"
RUN = Path("/run/nox-os")
#: The window itself (root, 0644: no secret in it).
SETUP_MODE = RUN / "setup-mode"
#: This boot's setup state: root:nox-os-setup 0750.
SETUP = RUN / "setup"
CONSOLE = SETUP / "console.json"  # root:nox-os-setup 0640 - what the screen shows
CLAIM_CODE = SETUP / "claim-code"  # root:nox-os-setup 0640 - the portal shows it
NETWORKS = SETUP / "networks.json"  # root:nox-os-setup 0640 - the portal's Wi-Fi list
STATUS = SETUP / "status.json"  # root:nox-os-setup 0640 - no secrets
HOTSPOT_STATE = SETUP / "hotspot.json"  # root 0644 - the firewall's setup ports
BLINK = SETUP / "blink"  # root 0600 - for the LED (O5)
PENDING = SETUP / "pending.json"  # root 0600 - the root helper's, applied by the claim
PORTAL_DIR = SETUP / "portal"  # nox-setup 0700 - the portal's requests
REQUEST = PORTAL_DIR / "request.json"
HELPER_SOCKET = RUN / "helper" / "helper.sock"
STATE = Path("/var/lib/nox-os")
CLAIM = STATE / "claim.json"  # root 0600: the claim code's hash
HOTSPOT_PSK = STATE / "hotspot-psk"  # root 0600: the hotspot password from the setup card
COUNTRY = STATE / "country"  # root 0600: the Wi-Fi country from the setup card
RESET_FLAG = STATE / "reset-maintenance"
PASSPHRASE = STATE / "maintenance"
#: Root's mark that the device has an owner (0644: Nox and the firewall read it).
CLAIMED = Path("/var/lib/nox-os-claim/claimed")
#: Test images only (os/tools/qemu-smoke.sh): a shorter window. check-image.sh refuses an image
#: that has this folder.
TESTING_WINDOW = Path("/etc/nox-os/testing/setup-window-s")
NM_SYSTEM = Path("/etc/NetworkManager/system-connections")
NM_RUNTIME = Path("/run/NetworkManager/system-connections")
WIFI_PROFILE = "nox-wifi"
HOTSPOT_PROFILE = "nox-setup-hotspot"
WIFI_KEYFILE = NM_SYSTEM / f"{WIFI_PROFILE}.nmconnection"
HOTSPOT_KEYFILE = NM_RUNTIME / f"{HOTSPOT_PROFILE}.nmconnection"
WIFI_COUNTRY_CONF = Path("/etc/modprobe.d/nox-os-wifi-country.conf")
ZONEINFO = Path("/usr/share/zoneinfo")
SETUP_GROUP = "nox-os-setup"
PORTAL_USER = "nox-setup"
PORTAL_UNIT = "nox-setup-portal.service"
TIMER_UNIT = "nox-setup.timer"
FIREWALL_UNIT = "nox-firewall.service"
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")

# ---- rules ---------------------------------------------------------------------------------

WINDOW_S = 30 * 60
#: A shortened window (test images) is at least this long.
MIN_TEST_WINDOW_S = 60
#: How long after boot Nox waits for Ethernet or the provisioned Wi-Fi before the hotspot.
NO_NETWORK_S = 90
#: How long joining the home Wi-Fi from the portal may take before the hotspot comes back.
JOIN_TIMEOUT_S = 60
HOTSPOT_ADDRESS = "10.42.0.1"
HOTSPOT_PREFIX = 24
HOTSPOT_CHANNEL = 6
MAX_PROVISION_BYTES = 16 * 1024
MAX_REQUEST_BYTES = 4 * 1024
MAX_NETWORKS = 30
SCHEMA_VERSION = 1
#: The locales the image has generated (stage-nox/04-firstboot/00-run.sh).
LOCALES = ("de_DE", "en_GB", "en_US")
#: Crockford's base32: no I, L, O, U - nothing to confuse when typing it from a screen.
CLAIM_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CLAIM_LENGTH = 12
CLAIM_PREFIX = b"nox-claim-v1:"
HOTSPOT_DIGITS = 12
MIN_PIN = 6
MAX_PIN = 64
MIN_PASSPHRASE = 12
MAX_PASSPHRASE = 256

_HOSTNAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_TIMEZONE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]{0,30}(?:/[A-Za-z0-9][A-Za-z0-9_+-]{0,30}){0,2}")
_COUNTRY = re.compile(r"[A-Z]{2}")
_CLAIM_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_HEX64 = re.compile(r"[0-9a-fA-F]{64}")
_IFACE = re.compile(r"[a-z][a-z0-9]{0,14}")
_BOOT_ID = re.compile(r"[0-9a-f-]{36}")


class ProvisionError(ValueError):
    """The provisioning file is refused. `code` names the rule, never a value from the file."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---- the provisioning file -----------------------------------------------------------------


@dataclass(frozen=True)
class Wifi:
    ssid: str
    #: None: an open network.
    psk: str | None = field(default=None, repr=False)
    hidden: bool = False


@dataclass(frozen=True)
class Provision:
    hostname: str | None = None
    timezone: str | None = None
    locale: str | None = None
    country: str | None = None
    wifi: Wifi | None = None
    claim_hash: str | None = None
    hotspot_psk: str | None = field(default=None, repr=False)

    def fields(self) -> list[str]:
        """The names of what is set - what may be logged."""
        return [
            name
            for name in (
                "hostname",
                "timezone",
                "locale",
                "country",
                "wifi",
                "claim_hash",
                "hotspot_psk",
            )
            if getattr(self, name) is not None
        ]


_TOP = frozenset(
    {
        "$schema",
        "v",
        "hostname",
        "timezone",
        "locale",
        "country",
        "wifi",
        "claim_hash",
        "hotspot_psk",
    }
)
_WIFI = frozenset({"ssid", "psk", "hidden"})


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ProvisionError("duplicate_field")
        out[key] = value
    return out


def _no_constant(_name: str) -> Any:
    raise ProvisionError("not_json")


def _optional_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProvisionError(f"invalid_{key}")
    return value


def wpa_psk_ok(psk: str) -> bool:
    """A WPA2 passphrase (8-63 printable ASCII characters) or a raw key (64 hex digits)."""
    if _HEX64.fullmatch(psk):
        return True
    return 8 <= len(psk) <= 63 and all(0x20 <= ord(c) <= 0x7E for c in psk)


def ssid_ok(ssid: str) -> bool:
    raw = ssid.encode("utf-8", errors="strict")
    return 1 <= len(raw) <= 32 and not any(ord(c) < 0x20 or ord(c) == 0x7F for c in ssid)


def hostname_ok(name: str) -> bool:
    return bool(_HOSTNAME.fullmatch(name)) and not name.isdigit()


def timezone_ok(name: str) -> bool:
    return len(name) <= 64 and bool(_TIMEZONE.fullmatch(name)) and ".." not in name


def validate(raw: bytes) -> Provision:
    """The provisioning file, checked field by field (schema: os/provisioning/). Anything not in
    the schema is refused - a typo must not be silently ignored on a device without a screen."""
    if len(raw) > MAX_PROVISION_BYTES:
        raise ProvisionError("too_large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ProvisionError("not_utf8") from None
    text = text.removeprefix("\ufeff")  # an editor's byte-order mark
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except ProvisionError:
        raise
    except (ValueError, RecursionError):
        raise ProvisionError("not_json") from None
    if not isinstance(data, dict):
        raise ProvisionError("not_an_object")
    unknown = set(data) - _TOP
    if unknown:
        raise ProvisionError("unknown_field")
    version = data.get("v")
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        raise ProvisionError("unsupported_version")
    if "$schema" in data and not isinstance(data["$schema"], str):
        raise ProvisionError("invalid_schema")
    hostname = _optional_str(data, "hostname")
    if hostname is not None and not hostname_ok(hostname):
        raise ProvisionError("invalid_hostname")
    timezone = _optional_str(data, "timezone")
    if timezone is not None and not timezone_ok(timezone):
        raise ProvisionError("invalid_timezone")
    locale = _optional_str(data, "locale")
    if locale is not None and locale not in LOCALES:
        raise ProvisionError("invalid_locale")
    country = _optional_str(data, "country")
    if country is not None and not _COUNTRY.fullmatch(country):
        raise ProvisionError("invalid_country")
    claim_hash = _optional_str(data, "claim_hash")
    if claim_hash is not None and not _CLAIM_HASH.fullmatch(claim_hash):
        raise ProvisionError("invalid_claim_hash")
    hotspot_psk = _optional_str(data, "hotspot_psk")
    if hotspot_psk is not None and not (
        8 <= len(hotspot_psk) <= 63 and all(0x21 <= ord(c) <= 0x7E for c in hotspot_psk)
    ):
        raise ProvisionError("invalid_hotspot_psk")
    wifi = None
    section = data.get("wifi")
    if section is not None:
        if not isinstance(section, dict) or set(section) - _WIFI:
            raise ProvisionError("invalid_wifi")
        ssid = section.get("ssid")
        if not isinstance(ssid, str) or not ssid_ok(ssid):
            raise ProvisionError("invalid_wifi_ssid")
        psk = section.get("psk")
        if psk is not None and (not isinstance(psk, str) or not wpa_psk_ok(psk)):
            raise ProvisionError("invalid_wifi_psk")
        hidden = section.get("hidden", False)
        if not isinstance(hidden, bool):
            raise ProvisionError("invalid_wifi_hidden")
        if country is None:
            # Raspberry Pi OS keeps Wi-Fi blocked until a regulatory country is set.
            raise ProvisionError("country_required")
        wifi = Wifi(ssid=ssid, psk=psk, hidden=hidden)
    return Provision(
        hostname=hostname,
        timezone=timezone,
        locale=locale,
        country=country,
        wifi=wifi,
        claim_hash=claim_hash,
        hotspot_psk=hotspot_psk,
    )


# ---- claim codes and hotspot passwords -----------------------------------------------------


def normalize_claim_code(text: str) -> str | None:
    """What the person typed -> the code, or None. Spaces and dashes do not count, case does not
    matter, and the letters Crockford's alphabet leaves out read as the digits they look like."""
    code = re.sub(r"[\s-]", "", text).upper().translate(str.maketrans("ILO", "110"))
    if not 8 <= len(code) <= 32 or any(c not in CLAIM_ALPHABET for c in code):
        return None
    return code


def claim_hash(code: str) -> str:
    """`sha256:<hex>` of the normalized code, domain-separated (os/README.md, provisioning)."""
    return "sha256:" + hashlib.sha256(CLAIM_PREFIX + code.encode("ascii")).hexdigest()


def new_claim_code() -> str:
    return "".join(secrets.choice(CLAIM_ALPHABET) for _ in range(CLAIM_LENGTH))


def show_claim_code(code: str) -> str:
    return "-".join(code[i : i + 4] for i in range(0, len(code), 4))


def new_hotspot_psk() -> str:
    """12 random digits: what a screen shows and an LED can blink (about 40 bits)."""
    return "".join(str(secrets.randbelow(10)) for _ in range(HOTSPOT_DIGITS))


def hotspot_ssid(machine_id: str) -> str:
    """`Nox-Setup-1A2B`: per device, stable across boots, and no part of the machine id."""
    digest = hashlib.sha256(b"nox-setup-ssid:" + machine_id.strip().encode("ascii", "replace"))
    return "Nox-Setup-" + digest.hexdigest()[:4].upper()


# ---- NetworkManager keyfiles ---------------------------------------------------------------


def _keyfile_string(value: str) -> str:
    """A GKeyFile string value: backslashes and every space escaped, so nothing is trimmed."""
    if any(c in value for c in "\n\r\x00"):
        raise ValueError("no line breaks in a keyfile value")
    return value.replace("\\", "\\\\").replace(" ", "\\s").replace("\t", "\\t")


def _keyfile_ssid(ssid: str) -> str:
    """The SSID as a byte list (`83;112;...;`): NetworkManager reads any name back exactly,
    whatever characters it has."""
    return "".join(f"{byte};" for byte in ssid.encode("utf-8"))


def wifi_keyfile(wifi: Wifi, profile_uuid: str) -> str:
    lines = [
        "# Nox-OS: the home Wi-Fi (from the setup card or the setup portal). Root only.",
        "[connection]",
        f"id={WIFI_PROFILE}",
        f"uuid={profile_uuid}",
        "type=wifi",
        "autoconnect=true",
        "autoconnect-priority=10",
        "",
        "[wifi]",
        "mode=infrastructure",
        f"ssid={_keyfile_ssid(wifi.ssid)}",
    ]
    if wifi.hidden:
        lines.append("hidden=true")
    if wifi.psk is not None:
        lines += [
            "",
            "[wifi-security]",
            "key-mgmt=wpa-psk",
            f"psk={_keyfile_string(wifi.psk)}",
        ]
    lines += [
        "",
        "[ipv4]",
        "method=auto",
        "",
        "[ipv6]",
        "method=auto",
        "addr-gen-mode=stable-privacy",
        "",
    ]
    return "\n".join(lines)


def hotspot_keyfile(ssid: str, psk: str, iface: str, profile_uuid: str) -> str:
    """The setup hotspot: WPA2 (CCMP) only, NetworkManager's shared mode (DHCP and DNS on
    10.42.0.1 - every name answers with the portal, dnsmasq-shared.d/50-nox-setup.conf), IPv6 off.
    In /run: gone at the next boot."""
    return "\n".join(
        [
            "# Nox-OS setup hotspot (nox_setup.py). In memory only; root only.",
            "[connection]",
            f"id={HOTSPOT_PROFILE}",
            f"uuid={profile_uuid}",
            "type=wifi",
            f"interface-name={iface}",
            "autoconnect=false",
            "",
            "[wifi]",
            "mode=ap",
            f"ssid={_keyfile_ssid(ssid)}",
            "band=bg",
            f"channel={HOTSPOT_CHANNEL}",
            "",
            "[wifi-security]",
            "key-mgmt=wpa-psk",
            "proto=rsn",
            "pairwise=ccmp",
            "group=ccmp",
            f"psk={_keyfile_string(psk)}",
            "",
            "[ipv4]",
            "method=shared",
            f"address1={HOTSPOT_ADDRESS}/{HOTSPOT_PREFIX}",
            "",
            "[ipv6]",
            "method=disabled",
            "",
        ]
    )


def nmcli_fields(line: str) -> list[str]:
    """One line of `nmcli -t`: fields split at unescaped colons, `\\:` and `\\\\` undone."""
    out, current, escaped = [], [], False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            out.append("".join(current))
            current = []
        else:
            current.append(char)
    out.append("".join(current))
    return out


def parse_scan(text: str) -> list[dict[str, Any]]:
    """`nmcli -t -f SSID,SIGNAL,SECURITY device wifi list` -> the portal's list: named networks,
    each once (the strongest), strongest first."""
    best: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        parts = nmcli_fields(line)
        if len(parts) != 3:
            continue
        ssid, signal, security = parts
        if not ssid or not ssid_ok(ssid) or not signal.isdigit():
            continue
        entry = {
            "ssid": ssid,
            "signal": min(int(signal), 100),
            "open": security.strip() in ("", "--"),
        }
        if ssid not in best or entry["signal"] > best[ssid]["signal"]:
            best[ssid] = entry
    return sorted(best.values(), key=lambda e: (-e["signal"], e["ssid"]))[:MAX_NETWORKS]


@dataclass(frozen=True)
class Device:
    name: str
    type: str
    state: str
    connection: str


def parse_devices(text: str) -> list[Device]:
    """`nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status`."""
    out = []
    for line in text.splitlines():
        parts = nmcli_fields(line)
        if len(parts) == 4:
            out.append(Device(*parts))
    return out


def join_error(stderr: str) -> str:
    """Why joining the home Wi-Fi failed, as a code the portal words."""
    text = stderr.lower()
    if "secrets were required" in text or "802-1x" in text or "psk" in text:
        return "wrong_password"
    if "no network with ssid" in text or "not found" in text:
        return "not_found"
    return "wifi_failed"


# ---- the system ----------------------------------------------------------------------------


def _run(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False, timeout=timeout)  # noqa: S603


def _uptime() -> float:
    return time.clock_gettime(time.CLOCK_BOOTTIME)  # type: ignore[attr-defined,unused-ignore]


def _gid(name: str) -> int | None:
    try:
        import grp  # noqa: PLC0415 - Linux only

        return grp.getgrnam(name).gr_gid
    except (ImportError, KeyError):
        return None


def _uid(name: str) -> int | None:
    try:
        import pwd  # noqa: PLC0415 - Linux only

        return pwd.getpwnam(name).pw_uid
    except (ImportError, KeyError):
        return None


@dataclass
class System:
    """Everything this program reads, changes and runs, in one place (tests replace it)."""

    run: Callable[..., subprocess.CompletedProcess[str]] = _run
    uptime: Callable[[], float] = _uptime
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    root: Path = Path("/")
    gid_of: Callable[[str], int | None] = _gid
    uid_of: Callable[[str], int | None] = _uid
    #: Who owns what only root may create (tests run as someone else).
    root_uid: int = 0
    #: chown is root's; tests skip it.
    chown: bool = True

    def path(self, path: Path) -> Path:
        return self.root / path.relative_to("/")

    def write(
        self,
        path: Path,
        data: str | bytes,
        mode: int,
        *,
        group: str | None = None,
        owner: str | None = None,
    ) -> None:
        """Atomically, never through a link, with its mode (and owner) before it has content."""
        target = self.path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
        try:
            if self.chown and (group or owner):
                uid = self.uid_of(owner) if owner else 0
                gid = self.gid_of(group) if group else 0
                if uid is None or gid is None:
                    raise OSError(f"no such user or group: {owner or group}")
                os.fchown(fd, uid, gid)
            os.fchmod(fd, mode) if hasattr(os, "fchmod") else None
            os.write(fd, data.encode("utf-8") if isinstance(data, str) else data)
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
        os.close(fd)
        if not hasattr(os, "fchmod"):
            os.chmod(tmp, mode)
        os.replace(tmp, target)

    def read(self, path: Path, limit: int = 64 * 1024) -> bytes:
        fd = os.open(self.path(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise OSError("not a small regular file")
            with os.fdopen(fd, "rb", closefd=False) as handle:
                return handle.read(limit + 1)
        finally:
            os.close(fd)

    def root_file(self, path: Path) -> bool:
        """A plain file that belongs to root (nobody else can put it there)."""
        try:
            info = self.path(path).lstat()
        except OSError:
            return False
        return stat.S_ISREG(info.st_mode) and info.st_uid == self.root_uid

    def exists(self, path: Path) -> bool:
        return os.path.lexists(self.path(path))

    def remove(self, path: Path) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path(path).unlink()

    def systemctl(self, *args: str) -> bool:
        return self.run("systemctl", *args).returncode == 0

    def nmcli(self, *args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
        return self.run("nmcli", *args, timeout=timeout)


def _json(system: System, path: Path) -> dict[str, Any]:
    try:
        data = json.loads(system.read(path))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def boot_id(system: System) -> str:
    try:
        value = system.read(BOOT_ID, 64).decode("ascii").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    return value if _BOOT_ID.fullmatch(value) else ""


# ---- the window ----------------------------------------------------------------------------


def claimed(system: System) -> bool:
    return system.root_file(CLAIMED)


def window_seconds(system: System) -> int:
    """30 minutes - shorter only on a test image (TESTING_WINDOW, root's, never shipped)."""
    if not system.root_file(TESTING_WINDOW):
        return WINDOW_S
    try:
        value = int(system.read(TESTING_WINDOW, 32).decode("ascii").strip())
    except (OSError, ValueError, UnicodeDecodeError):
        return WINDOW_S
    return max(MIN_TEST_WINDOW_S, min(value, WINDOW_S))


def setup_mode(system: System) -> dict[str, Any] | None:
    """The open window, or None: root's file, this boot's, its end not reached. The same rule as
    `in_setup_mode` in nox_os_helper.py and `setup_window_open` in nox_firewall.py."""
    if not system.root_file(SETUP_MODE):
        return None
    mode = _json(system, SETUP_MODE)
    until = mode.get("until")
    current = boot_id(system)
    if (
        mode.get("v") != 1
        or not current
        or mode.get("boot_id") != current
        or isinstance(until, bool)
        or not isinstance(until, (int, float))
        or system.uptime() >= until
    ):
        return None
    return mode


def open_window(system: System, reason: str) -> dict[str, Any]:
    """At most `window_seconds` after power-on. A window already open is kept, never extended."""
    existing = setup_mode(system)
    if existing is not None:
        return existing
    mode = {"v": 1, "boot_id": boot_id(system), "until": window_seconds(system), "reason": reason}
    system.write(SETUP_MODE, json.dumps(mode) + "\n", 0o644)
    return mode


# ---- status for the portal and the console (no secrets in status.json) --------------------


def read_status(system: System) -> dict[str, Any]:
    return _json(system, STATUS)


def write_status(system: System, **values: Any) -> dict[str, Any]:
    status = {**read_status(system), **values, "updated": int(system.clock())}
    system.write(STATUS, json.dumps(status) + "\n", 0o640, group=SETUP_GROUP)
    return status


# ---- step: boot ----------------------------------------------------------------------------


def secure_delete(system: System, path: Path) -> bool:
    """Overwrite the file's bytes with random ones, flush, truncate, delete, flush the folder.

    On a FAT file system on an SD card this is best effort (os/README.md): it overwrites the
    clusters the file has now, but the card's flash controller may keep older copies of those
    blocks until it reuses them, and an editor that saved the file twice left earlier clusters
    behind. What it does guarantee: the file is gone and its current clusters hold noise."""
    target = system.path(path)
    try:
        fd = os.open(target, os.O_RDWR | _NOFOLLOW)
    except FileNotFoundError:
        return True
    except OSError:
        with contextlib.suppress(OSError):
            target.unlink()
        return not os.path.lexists(target)
    try:
        size = os.fstat(fd).st_size
        os.lseek(fd, 0, os.SEEK_SET)
        left = size
        while left > 0:
            chunk = os.urandom(min(left, 4096))
            written = os.write(fd, chunk)
            left -= written
        os.fsync(fd)
        os.ftruncate(fd, 0)
        os.fsync(fd)
    finally:
        os.close(fd)
    target.unlink()
    with contextlib.suppress(OSError, AttributeError):
        folder = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
    return not os.path.lexists(target)


def _set_hostname(system: System, name: str) -> None:
    system.write(Path("/etc/hostname"), name + "\n", 0o644)
    hosts = system.path(Path("/etc/hosts"))
    try:
        lines = hosts.read_text("utf-8").splitlines()
    except OSError:
        lines = ["127.0.0.1\tlocalhost"]
    lines = [line for line in lines if not line.startswith("127.0.1.1")]
    lines.append(f"127.0.1.1\t{name}")
    system.write(Path("/etc/hosts"), "\n".join(lines) + "\n", 0o644)
    system.run("hostname", name)


def _set_timezone(system: System, name: str) -> bool:
    zone = system.path(ZONEINFO / name)
    if not zone.is_file() or ".." in Path(name).parts:
        return False
    link = system.path(Path("/etc/localtime"))
    tmp = link.with_name(".localtime.tmp")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    os.symlink(f"/usr/share/zoneinfo/{name}", tmp)
    os.replace(tmp, link)
    system.write(Path("/etc/timezone"), name + "\n", 0o644)
    return True


def _set_locale(system: System, name: str) -> None:
    system.write(Path("/etc/default/locale"), f"LANG={name}.UTF-8\n", 0o644)


def _set_country(system: System, country: str) -> None:
    system.write(COUNTRY, country + "\n", 0o600)
    system.write(
        WIFI_COUNTRY_CONF,
        "# Nox-OS: Wi-Fi regulatory country (from the setup card)\n"
        f"options cfg80211 ieee80211_regdom={country}\n",
        0o644,
    )


def import_provision(system: System) -> dict[str, Any]:
    """Import the provisioning file if there is one, then overwrite and delete it - also when it
    is refused (it may hold a Wi-Fi password; nobody could read the reason on the card anyway: it
    is on the screen and in the portal). Returns what to report - never a value from the file."""
    if not system.exists(PROVISION):
        return {}
    try:
        raw = system.read(PROVISION, MAX_PROVISION_BYTES)
        provision = validate(raw)
    except ProvisionError as exc:
        report: dict[str, Any] = {"imported": False, "error": exc.code}
    except OSError:
        report = {"imported": False, "error": "unreadable"}
    else:
        report = {"imported": True, "error": None, "fields": provision.fields()}
        if provision.hostname:
            _set_hostname(system, provision.hostname)
        if provision.timezone and not _set_timezone(system, provision.timezone):
            report["warning"] = "unknown_timezone"
        if provision.locale:
            _set_locale(system, provision.locale)
        if provision.country:
            _set_country(system, provision.country)
        if provision.wifi is not None:
            system.write(WIFI_KEYFILE, wifi_keyfile(provision.wifi, str(uuid.uuid4())), 0o600)
        if provision.hotspot_psk:
            system.write(HOTSPOT_PSK, provision.hotspot_psk + "\n", 0o600)
        if provision.claim_hash:
            if claimed(system):
                report["warning"] = "already_claimed"
            else:
                record = {"v": 1, "hash": provision.claim_hash, "source": "card"}
                system.write(CLAIM, json.dumps(record) + "\n", 0o600)
    # macOS leaves a metadata twin next to a file it copied; it goes too.
    system.remove(BOOT / "._nox-provision.json")
    report["deleted"] = secure_delete(system, PROVISION)
    return report


def _machine_id(system: System) -> str:
    try:
        return system.read(Path("/etc/machine-id"), 64).decode("ascii", "replace").strip()
    except OSError:
        return "unknown"


def prepare_secrets(system: System) -> dict[str, Any]:
    """This boot's hotspot password and - unless the setup card brought one - claim code."""
    card_psk = None
    if system.root_file(HOTSPOT_PSK):
        with contextlib.suppress(OSError, UnicodeDecodeError):
            card_psk = system.read(HOTSPOT_PSK, 128).decode("ascii").strip() or None
    psk = card_psk or new_hotspot_psk()
    claim = _json(system, CLAIM)
    code = None
    if claim.get("source") != "card" or not _CLAIM_HASH.fullmatch(str(claim.get("hash", ""))):
        code = new_claim_code()
        record = {"v": 1, "hash": claim_hash(code), "source": "device"}
        system.write(CLAIM, json.dumps(record) + "\n", 0o600)
        system.write(CLAIM_CODE, show_claim_code(code) + "\n", 0o640, group=SETUP_GROUP)
    if card_psk is None:
        system.write(BLINK, psk + "\n", 0o600)
    console = {
        "ssid": hotspot_ssid(_machine_id(system)),
        "psk": psk,
        "psk_from_card": card_psk is not None,
        "claim_code": show_claim_code(code) if code else None,
    }
    system.write(CONSOLE, json.dumps(console) + "\n", 0o640, group=SETUP_GROUP)
    return console


def boot(system: System) -> int:
    """nox-firstboot.service: before the network and before Nox."""
    for folder, mode, group in ((RUN, 0o755, None), (SETUP, 0o750, SETUP_GROUP)):
        target = system.path(folder)
        target.mkdir(parents=True, exist_ok=True)
        if system.chown:
            gid = system.gid_of(group) if group else 0
            if gid is not None:
                os.chown(target, 0, gid)
        os.chmod(target, mode)
    portal = system.path(PORTAL_DIR)
    portal.mkdir(exist_ok=True)
    if system.chown:
        uid, gid = system.uid_of(PORTAL_USER), system.gid_of(PORTAL_USER)
        if uid is not None and gid is not None:
            os.chown(portal, uid, gid)
    os.chmod(portal, 0o700)
    system.path(STATE).mkdir(parents=True, exist_ok=True)
    os.chmod(system.path(STATE), 0o700)
    report = import_provision(system)
    if report:
        print(
            "nox-setup: provisioning file "
            + (
                f"imported ({', '.join(report.get('fields', []))})"
                if report.get("imported")
                else f"refused ({report.get('error')})"
            )
            + (" and deleted" if report.get("deleted") else ", NOT deleted"),
            file=sys.stderr,
        )
    write_status(system, state="idle", provision=report or None, hotspot="down")
    is_claimed = claimed(system)
    if not is_claimed:
        mode = open_window(system, "unclaimed")
        prepare_secrets(system)
        print(
            f"nox-setup: unclaimed - setup open until {int(mode['until'])} s after boot",
            file=sys.stderr,
        )
    elif system.root_file(RESET_FLAG):
        open_window(system, "reset-maintenance")
        print("nox-setup: maintenance passphrase reset - setup open", file=sys.stderr)
    else:
        system.remove(CLAIM)  # one-time: a claimed device keeps no claim code
    return 0


# ---- step: tick (the hotspot) --------------------------------------------------------------


def devices(system: System) -> list[Device] | None:
    """NetworkManager's devices, or None while it cannot answer (still starting): nothing is
    decided on a guess."""
    shown = system.nmcli("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status", timeout=20)
    return parse_devices(shown.stdout) if shown.returncode == 0 else None


def wifi_device(found: list[Device]) -> Device | None:
    for device in found:
        if device.type == "wifi" and _IFACE.fullmatch(device.name):
            return device
    return None


def online(found: list[Device]) -> bool:
    """Ethernet or Wi-Fi connected to a network that is not the setup hotspot."""
    return any(
        d.type in ("ethernet", "wifi")
        and d.state.startswith("connected")
        and d.connection != HOTSPOT_PROFILE
        for d in found
    )


def hotspot_up(found: list[Device]) -> bool:
    return any(d.connection == HOTSPOT_PROFILE and d.state.startswith("connected") for d in found)


def card_claim_code(system: System) -> bool:
    return _json(system, CLAIM).get("source") == "card"


def want_hotspot(system: System, found: list[Device]) -> bool:
    """Up when Nox found no network in time - or right away when the claim code was made on the
    device and the portal is the only place without a screen to learn it (an Ethernet cable does
    not need the Wi-Fi radio). Never while the radio is the home network's."""
    radio = wifi_device(found)
    if (
        radio is not None
        and radio.state.startswith("connected")
        and radio.connection != HOTSPOT_PROFILE
    ):
        return False
    if not card_claim_code(system):
        return True
    return not online(found) and system.uptime() >= NO_NETWORK_S


def scan(system: System, iface: str) -> None:
    shown = system.nmcli(
        "-t",
        "-f",
        "SSID,SIGNAL,SECURITY",
        "device",
        "wifi",
        "list",
        "--rescan",
        "yes",
        "ifname",
        iface,
        timeout=45,
    )
    networks = parse_scan(shown.stdout) if shown.returncode == 0 else []
    system.write(NETWORKS, json.dumps({"networks": networks}) + "\n", 0o640, group=SETUP_GROUP)


def start_hotspot(system: System, found: list[Device]) -> bool:
    """Write the hotspot's profile (in /run) and bring it up on the Wi-Fi radio - after a scan, as
    the radio cannot look for networks while it is the hotspot."""
    console = _json(system, CONSOLE)
    ssid, psk = console.get("ssid"), console.get("psk")
    if not isinstance(ssid, str) or not isinstance(psk, str):
        console = prepare_secrets(system)
        ssid, psk = console["ssid"], console["psk"]
    radio = wifi_device(found)
    iface = radio.name if radio is not None else "wlan0"
    system.write(HOTSPOT_KEYFILE, hotspot_keyfile(ssid, psk, iface, str(uuid.uuid4())), 0o600)
    if radio is None:
        write_status(system, hotspot="no_wifi_device")
        return False
    system.run("rfkill", "unblock", "wifi", timeout=10)
    scan(system, iface)
    system.nmcli("connection", "load", str(HOTSPOT_KEYFILE), timeout=20)
    up = system.nmcli("--wait", "30", "connection", "up", HOTSPOT_PROFILE, timeout=45)
    if up.returncode != 0:
        write_status(system, hotspot="failed")
        print("nox-setup: the setup hotspot did not start", file=sys.stderr)
        return False
    system.write(
        HOTSPOT_STATE,
        json.dumps({"iface": iface, "address": HOTSPOT_ADDRESS, "prefix": HOTSPOT_PREFIX}) + "\n",
        0o644,
    )
    system.systemctl("restart", FIREWALL_UNIT)
    system.systemctl("start", PORTAL_UNIT)
    write_status(system, hotspot="up", ssid=ssid)
    print(f"nox-setup: setup hotspot up on {iface}", file=sys.stderr)
    return True


def stop_hotspot(system: System) -> None:
    system.systemctl("stop", PORTAL_UNIT)
    system.nmcli("connection", "delete", HOTSPOT_PROFILE, timeout=30)
    system.remove(HOTSPOT_KEYFILE)
    if system.exists(HOTSPOT_STATE):
        system.remove(HOTSPOT_STATE)
        system.systemctl("restart", FIREWALL_UNIT)
    write_status(system, hotspot="down")


def tick(system: System) -> int:
    mode = setup_mode(system)
    if mode is None:
        if system.exists(SETUP_MODE) or system.exists(CONSOLE):
            close(system, "expired")
        else:
            system.systemctl("stop", "--no-block", TIMER_UNIT)  # nothing to watch this boot
        return 0
    if mode.get("reason") == "unclaimed" and claimed(system):
        close(system, "claimed")
        return 0
    if mode.get("reason") != "unclaimed":
        return 0
    status = read_status(system)
    if status.get("state") == "joining":
        return 0
    found = devices(system)
    if found is None:
        return 0  # NetworkManager is not up yet: the next step decides
    if hotspot_up(found):
        system.systemctl("start", PORTAL_UNIT)  # no-op while it runs
        return 0
    if status.get("state") == "joined":
        return 0  # the phone went back to the home network to claim
    if want_hotspot(system, found) and status.get("hotspot") != "no_wifi_device":
        start_hotspot(system, found)
    return 0


# ---- step: apply (what the portal asked for) ----------------------------------------------


@dataclass(frozen=True)
class PortalRequest:
    wifi: Wifi | None
    #: The claim code typed into the portal: without the right one, nothing is applied.
    claim_code: str = field(default="", repr=False)
    #: The phone's address as the portal saw it (the root helper slows wrong codes per address).
    peer: str = ""
    pin: str | None = field(default=None, repr=False)
    passphrase: str | None = field(default=None, repr=False)


def parse_portal_request(raw: bytes) -> PortalRequest:
    """The portal checked all of it; root checks it again, the same way."""
    if len(raw) > MAX_REQUEST_BYTES:
        raise ProvisionError("too_large")
    try:
        data = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_no_constant
        )
    except (ValueError, RecursionError, UnicodeDecodeError):
        raise ProvisionError("not_json") from None
    allowed = {"v", "wifi", "pin", "passphrase", "claim_code", "peer"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise ProvisionError("unknown_field")
    if data.get("v") != 1 or isinstance(data.get("v"), bool):
        raise ProvisionError("unsupported_version")
    wifi = None
    section = data.get("wifi")
    if section is not None:
        if not isinstance(section, dict) or set(section) - _WIFI:
            raise ProvisionError("invalid_wifi")
        ssid, psk = section.get("ssid"), section.get("psk")
        if not isinstance(ssid, str) or not ssid_ok(ssid):
            raise ProvisionError("invalid_wifi_ssid")
        if psk is not None and (not isinstance(psk, str) or not wpa_psk_ok(psk)):
            raise ProvisionError("invalid_wifi_psk")
        hidden = section.get("hidden", False)
        if not isinstance(hidden, bool):
            raise ProvisionError("invalid_wifi_hidden")
        wifi = Wifi(ssid=ssid, psk=psk, hidden=hidden)
    pin = data.get("pin")
    if pin is not None and (
        not isinstance(pin, str) or not MIN_PIN <= len(pin) <= MAX_PIN or not pin.isprintable()
    ):
        raise ProvisionError("invalid_pin")
    passphrase = data.get("passphrase")
    if passphrase is not None and (
        not isinstance(passphrase, str)
        or not MIN_PASSPHRASE <= len(passphrase) <= MAX_PASSPHRASE
        or any(c in passphrase for c in "\x00\r\n")
    ):
        raise ProvisionError("invalid_passphrase")
    code = normalize_claim_code(str(data.get("claim_code", "")))
    if code is None:
        raise ProvisionError("claim_wrong")
    peer = data.get("peer")
    if not isinstance(peer, str) or not re.fullmatch(r"[0-9a-fA-F:.]{2,45}", peer):
        raise ProvisionError("invalid_peer")
    return PortalRequest(wifi=wifi, claim_code=code, peer=peer, pin=pin, passphrase=passphrase)


def take_request(system: System) -> PortalRequest | None:
    """Read the portal's request and delete it at once. Only a file the portal's own user wrote
    counts; a link or anything else is removed unread."""
    target = system.path(REQUEST)
    try:
        info = target.lstat()
    except OSError:
        return None
    owner = system.uid_of(PORTAL_USER)
    try:
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner:
            raise ProvisionError("not_the_portals")
        raw = system.read(REQUEST, MAX_REQUEST_BYTES)
        return parse_portal_request(raw)
    except (ProvisionError, OSError) as exc:
        write_status(system, error=getattr(exc, "code", "unreadable"))
        return None
    finally:
        system.remove(REQUEST)


def ask_helper(system: System, lines: list[str]) -> dict[str, Any]:
    """One request to the root helper, as root (it answers root and Nox's main process)."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(90)
    try:
        sock.connect(str(system.path(HELPER_SOCKET)))
        sock.sendall(("\n".join(lines) + "\n").encode("ascii"))
        sock.shutdown(socket.SHUT_WR)
        raw = sock.makefile("rb").readline(64 * 1024)
    finally:
        sock.close()
    answer = json.loads(raw)
    return answer if isinstance(answer, dict) else {}


def _b64(secret: str) -> str:
    return base64.b64encode(secret.encode("utf-8")).decode("ascii")


def join_wifi(system: System, wifi: Wifi) -> tuple[bool, str]:
    system.write(WIFI_KEYFILE, wifi_keyfile(wifi, str(uuid.uuid4())), 0o600)
    system.nmcli("connection", "load", str(WIFI_KEYFILE), timeout=20)
    done = system.nmcli(
        "--wait",
        str(JOIN_TIMEOUT_S),
        "connection",
        "up",
        WIFI_PROFILE,
        timeout=JOIN_TIMEOUT_S + 30,
    )
    if done.returncode == 0:
        return True, ""
    return False, join_error(done.stderr)


def apply(
    system: System,
    helper: Callable[[System, list[str]], dict[str, Any]] = ask_helper,
) -> int:
    request = take_request(system)
    if request is None:
        return 0
    if setup_mode(system) is None or claimed(system):
        write_status(system, error="window_closed")
        return 0
    # The claim code first, checked by the root helper: with a wrong one nothing at all happens -
    # no PIN or passphrase kept, no Wi-Fi changed. With the right one they are only kept: the
    # claim applies them, in the same step that marks the device claimed.
    lines = [f"stage {request.peer}", f"code {_b64(request.claim_code)}"]
    if request.pin:
        lines.append(f"pin {_b64(request.pin)}")
    if request.passphrase:
        lines.append(f"new {_b64(request.passphrase)}")
    try:
        answer = helper(system, lines)
    except (OSError, ValueError):
        answer = {"ok": False, "code": "helper_unreachable"}
    if answer.get("ok") is not True:
        write_status(system, error=str(answer.get("code", "stage_failed"))[:40])
        return 0
    staged = answer.get("pending_pin") is True and answer.get("pending_passphrase") is True
    write_status(system, security_done=staged, error=None)
    if request.wifi is None:
        write_status(system, state="ready")
        return 0
    write_status(system, state="joining", tried_ssid=request.wifi.ssid)
    # The portal's answer has to reach the phone before the hotspot goes.
    system.sleep(3)
    stop_hotspot(system)
    ok, error = join_wifi(system, request.wifi)
    if ok:
        write_status(system, state="joined", error=None)
        system.systemctl("restart", FIREWALL_UNIT)
        print("nox-setup: joined the home Wi-Fi", file=sys.stderr)
        return 0
    print(f"nox-setup: joining the home Wi-Fi failed ({error}); hotspot back", file=sys.stderr)
    system.nmcli("connection", "down", WIFI_PROFILE, timeout=30)
    write_status(system, state="failed", error=error)
    if setup_mode(system) is not None:
        start_hotspot(system, devices(system) or [])
    return 0


# ---- step: close ---------------------------------------------------------------------------


def close(system: System, reason: str) -> int:
    """End setup mode: the window first (so nothing it grants holds a moment longer), then the
    hotspot and the portal, then every secret of the setup, then the firewall."""
    system.remove(SETUP_MODE)
    stop_hotspot(system)
    for path in (CLAIM_CODE, CONSOLE, BLINK, PENDING, NETWORKS, REQUEST):
        system.remove(path)
    if claimed(system) or not card_claim_code(system):
        system.remove(CLAIM)  # a claimed device keeps none; a device code is this boot's only
    write_status(system, state="closed", reason=reason, hotspot="down")
    system.systemctl("restart", FIREWALL_UNIT)
    system.systemctl("stop", "--no-block", TIMER_UNIT)
    print(f"nox-setup: setup closed ({reason})", file=sys.stderr)
    return 0


def describe(system: System) -> dict[str, Any]:
    mode = setup_mode(system)
    return {
        "claimed": claimed(system),
        "setup": mode is not None,
        "seconds_left": max(0, int(mode["until"] - system.uptime())) if mode else None,
        "reason": mode.get("reason") if mode else None,
        "status": {k: v for k, v in read_status(system).items() if k != "provision"},
    }


@contextlib.contextmanager
def _locked(system: System) -> Iterator[None]:
    """One step at a time (boot, tick, apply and close may meet)."""
    path = system.path(RUN / "setup.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            import fcntl  # noqa: PLC0415 - POSIX only

            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:
            pass
        yield
    finally:
        os.close(fd)


def main(argv: list[str]) -> int:
    system = System()
    command = argv[1] if len(argv) >= 2 else ""
    if command == "status" and len(argv) == 2:
        print(json.dumps(describe(system), indent=2))
        return 0
    if command == "close" and len(argv) <= 3:
        reason = argv[2] if len(argv) == 3 and re.fullmatch(r"[a-z-]{1,20}", argv[2]) else "closed"
        with _locked(system):
            return close(system, reason)
    steps: dict[str, Callable[[System], int]] = {"boot": boot, "tick": tick, "apply": apply}
    if command in steps and len(argv) == 2:
        with _locked(system):
            return steps[command](system)
    print("usage: nox_setup.py boot | tick | apply | close [reason] | status", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

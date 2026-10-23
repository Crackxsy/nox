"""Nox-OS captive portal: the page a phone sees on the setup hotspot (os/README.md).

    python3 -I -S nox_setup_portal.py [--bind 10.42.0.1] [--port 80]

It runs only while the setup hotspot is up (`nox-setup-portal.service`, started by nox_setup.py),
as the user `nox-setup` - no root, no capabilities but binding port 80, no network but the
hotspot's (systemd's IP filter) and Nox's loopback port for its certificate fingerprint. It
changes nothing itself: it writes one request file that root's `nox_setup.py apply` checks again
and carries out (home Wi-Fi, security PIN, maintenance passphrase).

What the phone gets:
  * any address that is not the portal's - the operating systems' captive-portal probes
    (`generate_204`, `hotspot-detect.html`, `connecttest.txt` ...) included - is redirected to
    the portal, so the phone opens it by itself;
  * one form, no JavaScript at all: the claim code (filled in when the device made it - it is shown
    on the Pi's screen too; from the setup card otherwise), the home Wi-Fi (from a scan made
    before the hotspot started, or typed), its password, the security PIN and the maintenance
    passphrase; or "Nox has a cable" to skip the Wi-Fi. Root applies nothing without the right
    claim code, and PIN and passphrase only with the claim itself;
  * then the next step: back to the home Wi-Fi, open `https://nox.local:47843/claim/` with the
    claim code.

Hardening: a strict Content-Security-Policy (no scripts, nothing from elsewhere), no-store, no
referrer, no framing; the form carries a token bound to the phone's address and the time (HMAC
with a key made at start), checked with the Origin and Sec-Fetch-Site headers; requests are
limited per address and in total; bodies are at most 4 KB; at most 16 connections at once, each
with a 10 s timeout. German and English, chosen from the phone's language.

Standard library only.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import ssl
import stat
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

#: Never through a link; bytes as they are (O_BINARY: the tests on Windows).
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)

RUN = Path("/run/nox-os")
SETUP_MODE = RUN / "setup-mode"
SETUP = RUN / "setup"
CONSOLE = SETUP / "console.json"
CLAIM_CODE = SETUP / "claim-code"
NETWORKS = SETUP / "networks.json"
STATUS = SETUP / "status.json"
REQUEST = SETUP / "portal" / "request.json"
LAN_INFO_URL = "https://127.0.0.1:47843/api/lan/info"
DASHBOARD_PORT = 47843
ADDRESS = "10.42.0.1"

MAX_BODY = 4096
MAX_CONNECTIONS = 16
TIMEOUT_S = 10
TOKEN_TTL_S = 30 * 60
MIN_PIN = 6
MAX_PIN = 64
MIN_PASSPHRASE = 12
MAX_PASSPHRASE = 256
OTHER = "__other__"
CABLE = "__cable__"

CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none'"
)
HEADERS = {
    "Content-Security-Policy": CSP,
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}

# ---- texts ---------------------------------------------------------------------------------

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "title": "Set up Nox",
        "intro": "This is your Nox. Connect it to your home network and protect it - "
        "everything stays on this device.",
        "closes": "Setup closes in about {minutes} minutes. If it closes, unplug the Pi's power "
        "and plug it in again.",
        "closed": "Setup has ended. Unplug the Pi's power and plug it in again to open it for "
        "another 30 minutes.",
        "wifi_legend": "Home Wi-Fi",
        "wifi_hint": "Networks Nox found before it opened this setup network.",
        "wifi_none_found": "Nox found no networks. Type the name of yours below.",
        "open_network": "open network",
        "other": "Another network (type its name)",
        "cable": "No Wi-Fi: Nox is connected by a network cable",
        "other_ssid": "Network name",
        "psk": "Wi-Fi password",
        "psk_hint": "Leave it empty only for an open network.",
        "security_legend": "Protect your Nox",
        "pin": "Security PIN",
        "pin_hint": "At least 6 characters. Nox asks for it before anything important.",
        "pin2": "Security PIN again",
        "passphrase": "Maintenance passphrase",
        "passphrase_hint": "At least 12 characters. Only needed if you ever switch on remote "
        "maintenance (SSH); write it down somewhere safe.",
        "passphrase2": "Maintenance passphrase again",
        "security_done": "A security PIN and a maintenance passphrase are kept for the claim; "
        "you can set others when you claim Nox.",
        "claim_legend": "Claim code",
        "claim_code": "Claim code",
        "claim_hint_device": "Nox made it for this setup; it is also on a screen plugged into "
        "the Pi. Write it down - you need it once more to claim Nox.",
        "claim_hint_card": "It is on your setup card.",
        "submit": "Connect",
        "errors_title": "Please check:",
        "err_network_missing": "Choose your home Wi-Fi, or that Nox has a cable.",
        "err_ssid_invalid": "Type the network's name (at most 32 characters).",
        "err_psk_missing": "Type the Wi-Fi password.",
        "err_psk_invalid": "A Wi-Fi password has 8 to 63 characters.",
        "err_pin_short": "The PIN needs at least 6 characters.",
        "err_pin_mismatch": "The two PINs are not the same.",
        "err_passphrase_short": "The passphrase needs at least 12 characters.",
        "err_passphrase_mismatch": "The two passphrases are not the same.",
        "err_claim_code": "Type the claim code.",
        "err_claim_wrong": "That claim code is not right - nothing was saved.",
        "err_claim_wait": "Wrong claim codes slow Nox down. Wait a moment, then try again.",
        "err_wrong_password": "Nox could not join {ssid}: the password was not accepted. "
        "Try again.",
        "err_not_found": "Nox could not find {ssid}. Is it in range?",
        "err_wifi_failed": "Nox could not join {ssid}. Try again.",
        "err_provision": "The setup file on the card was not used ({code}). Set Nox up here "
        "instead.",
        "err_saved": "Nox could not save that ({code}). Try again.",
        "forbidden": "This form has expired. Open the page again.",
        "rate_limited": "Too many attempts. Wait a minute, then try again.",
        "done_title": "Almost done",
        "done_wifi": "Nox now joins {ssid}. This setup network goes away for that.",
        "done_cable": "Nox stays on its cable.",
        "done_back": "1. Connect this phone to your home network again.",
        "done_open": "2. Open this address and enter the claim code:",
        "done_code": "Claim code:",
        "done_card": "Use the claim code from your setup card.",
        "done_fingerprint": "Your browser warns because Nox made its own certificate. "
        "Continue only if it shows this fingerprint:",
        "done_failed": "If Nox cannot join, this setup network comes back within about a "
        "minute and tells you why.",
        "done_router": "If nox.local does not open, look for the device named nox in your "
        "router and use its address.",
        "language": "Deutsch",
    },
    "de": {
        "title": "Nox einrichten",
        "intro": "Das ist dein Nox. Verbinde ihn mit deinem Heimnetz und schütze ihn - alles "
        "bleibt auf diesem Gerät.",
        "closes": "Die Einrichtung schließt in etwa {minutes} Minuten. Ist sie geschlossen, zieh "
        "kurz den Strom vom Pi und steck ihn wieder ein.",
        "closed": "Die Einrichtung ist beendet. Zieh kurz den Strom vom Pi und steck ihn wieder "
        "ein - dann ist sie wieder 30 Minuten offen.",
        "wifi_legend": "Heim-WLAN",
        "wifi_hint": "Netze, die Nox gefunden hat, bevor es dieses Einrichtungsnetz geöffnet hat.",
        "wifi_none_found": "Nox hat keine Netze gefunden. Gib den Namen deines Netzes unten ein.",
        "open_network": "offenes Netz",
        "other": "Anderes Netz (Namen eingeben)",
        "cable": "Kein WLAN: Nox hängt am Netzwerkkabel",
        "other_ssid": "Name des Netzes",
        "psk": "WLAN-Passwort",
        "psk_hint": "Nur bei einem offenen Netz leer lassen.",
        "security_legend": "Nox schützen",
        "pin": "Sicherheits-PIN",
        "pin_hint": "Mindestens 6 Zeichen. Nox fragt danach, bevor es etwas Wichtiges tut.",
        "pin2": "Sicherheits-PIN wiederholen",
        "passphrase": "Wartungs-Passphrase",
        "passphrase_hint": "Mindestens 12 Zeichen. Nur nötig, wenn du je die Fernwartung (SSH) "
        "einschaltest; schreib sie an einen sicheren Ort.",
        "passphrase2": "Wartungs-Passphrase wiederholen",
        "security_done": "Eine Sicherheits-PIN und eine Wartungs-Passphrase sind für die Übernahme "
        "vorgemerkt; beim Übernehmen kannst du andere festlegen.",
        "claim_legend": "Besitzer-Code",
        "claim_code": "Besitzer-Code",
        "claim_hint_device": "Nox hat ihn für diese Einrichtung gemacht; er steht auch auf einem "
        "Bildschirm am Pi. Schreib ihn auf - du brauchst ihn noch einmal zum Übernehmen.",
        "claim_hint_card": "Er steht auf deiner Einrichtungskarte.",
        "submit": "Verbinden",
        "errors_title": "Bitte prüfen:",
        "err_network_missing": "Wähle dein Heim-WLAN - oder dass Nox am Kabel hängt.",
        "err_ssid_invalid": "Gib den Namen des Netzes ein (höchstens 32 Zeichen).",
        "err_psk_missing": "Gib das WLAN-Passwort ein.",
        "err_psk_invalid": "Ein WLAN-Passwort hat 8 bis 63 Zeichen.",
        "err_pin_short": "Die PIN braucht mindestens 6 Zeichen.",
        "err_pin_mismatch": "Die beiden PINs sind nicht gleich.",
        "err_passphrase_short": "Die Passphrase braucht mindestens 12 Zeichen.",
        "err_passphrase_mismatch": "Die beiden Passphrasen sind nicht gleich.",
        "err_claim_code": "Gib den Besitzer-Code ein.",
        "err_claim_wrong": "Dieser Besitzer-Code stimmt nicht - es wurde nichts gespeichert.",
        "err_claim_wait": "Falsche Besitzer-Codes bremsen Nox. Warte kurz und versuch es dann "
        "noch einmal.",
        "err_wrong_password": "Nox konnte {ssid} nicht beitreten: Das Passwort wurde nicht "
        "angenommen. Versuch es noch einmal.",
        "err_not_found": "Nox hat {ssid} nicht gefunden. Ist es in Reichweite?",
        "err_wifi_failed": "Nox konnte {ssid} nicht beitreten. Versuch es noch einmal.",
        "err_provision": "Die Einrichtungsdatei auf der Karte wurde nicht verwendet ({code}). "
        "Richte Nox stattdessen hier ein.",
        "err_saved": "Nox konnte das nicht speichern ({code}). Versuch es noch einmal.",
        "forbidden": "Dieses Formular ist abgelaufen. Öffne die Seite neu.",
        "rate_limited": "Zu viele Versuche. Warte eine Minute und versuch es dann noch einmal.",
        "done_title": "Fast fertig",
        "done_wifi": "Nox verbindet sich jetzt mit {ssid}. Dieses Einrichtungsnetz verschwindet "
        "dafür.",
        "done_cable": "Nox bleibt am Kabel.",
        "done_back": "1. Verbinde dieses Handy wieder mit deinem Heimnetz.",
        "done_open": "2. Öffne diese Adresse und gib den Besitzer-Code ein:",
        "done_code": "Besitzer-Code:",
        "done_card": "Nimm den Besitzer-Code von deiner Einrichtungskarte.",
        "done_fingerprint": "Dein Browser warnt, weil Nox sein eigenes Zertifikat gemacht hat. "
        "Mach nur weiter, wenn er diesen Fingerabdruck zeigt:",
        "done_failed": "Kann Nox nicht beitreten, kommt dieses Einrichtungsnetz nach etwa einer "
        "Minute zurück und sagt, warum.",
        "done_router": "Öffnet nox.local nicht, such in deinem Router das Gerät nox und nimm "
        "seine Adresse.",
        "language": "English",
    },
}

CSS = """
:root { color-scheme: light dark; --fg: #1a1a1a; --bg: #ffffff; --muted: #4a4a4a;
  --line: #8a8a8a; --accent: #0b57d0; --error: #b3261e; --box: #f2f2f2; }
@media (prefers-color-scheme: dark) { :root { --fg: #f2f2f2; --bg: #121212; --muted: #c4c4c4;
  --line: #8a8a8a; --accent: #8ab4f8; --error: #f2b8b5; --box: #1f1f1f; } }
* { box-sizing: border-box; }
body { margin: 0; font: 1.0625rem/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  color: var(--fg); background: var(--bg); }
main { max-width: 34rem; margin: 0 auto; padding: 1rem 1rem 3rem; }
h1 { font-size: 1.6rem; margin: 0.5rem 0 0.75rem; }
p { margin: 0.5rem 0; }
.muted { color: var(--muted); }
fieldset { border: 1px solid var(--line); border-radius: 0.5rem; margin: 1.25rem 0;
  padding: 0.75rem 1rem 1rem; }
legend { font-weight: 600; padding: 0 0.25rem; }
label { display: block; font-weight: 600; margin-top: 0.9rem; }
.choice { display: flex; align-items: center; gap: 0.6rem; min-height: 2.75rem;
  font-weight: 400; margin: 0; }
.choice input { width: 1.4rem; height: 1.4rem; margin: 0; }
input[type=text], input[type=password] { display: block; width: 100%; min-height: 2.75rem;
  margin-top: 0.3rem; padding: 0.5rem 0.75rem; font: inherit; color: var(--fg);
  background: var(--bg); border: 1px solid var(--line); border-radius: 0.4rem; }
input[aria-invalid=true] { border: 2px solid var(--error); }
.hint { display: block; font-weight: 400; color: var(--muted); font-size: 0.95rem; }
.error { color: var(--error); font-weight: 600; }
button { display: block; width: 100%; min-height: 3rem; margin-top: 1.5rem; font: inherit;
  font-weight: 600; color: #fff; background: var(--accent); border: 0; border-radius: 0.5rem; }
@media (prefers-color-scheme: dark) { button { color: #121212; } }
a { color: var(--accent); }
:focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }
.box { background: var(--box); border-radius: 0.5rem; padding: 0.75rem 1rem; margin: 1rem 0; }
.code { font: 600 1.5rem/1.3 ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace;
  letter-spacing: 0.08em; word-break: break-all; }
.mono { font-family: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace;
  word-break: break-all; }
.summary { border: 2px solid var(--error); border-radius: 0.5rem; padding: 0.5rem 1rem;
  margin: 1rem 0; }
.lang { text-align: right; margin: 0; }
""".strip()


def pick_language(query: Mapping[str, list[str]], accept: str) -> str:
    asked = (query.get("lang") or [""])[0]
    if asked in STRINGS:
        return asked
    for part in accept.split(","):
        tag = part.split(";")[0].strip().lower()
        if tag[:2] in STRINGS:
            return tag[:2]
    return "en"


# ---- state from root -----------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return {}
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return {}
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = json.loads(handle.read(64 * 1024))
    except (OSError, ValueError):
        return {}
    finally:
        os.close(fd)
    return data if isinstance(data, dict) else {}


def _read_text(path: Path) -> str:
    try:
        return path.read_text("ascii").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _uptime() -> float:
    return time.clock_gettime(time.CLOCK_BOOTTIME)  # type: ignore[attr-defined,unused-ignore]


@dataclass
class State:
    """Where the portal reads root's setup state (tests point it elsewhere)."""

    setup_mode: Path = SETUP_MODE
    console: Path = CONSOLE
    claim_code: Path = CLAIM_CODE
    networks: Path = NETWORKS
    status: Path = STATUS
    request: Path = REQUEST
    uptime: Callable[[], float] = _uptime
    fingerprint: Callable[[], str] = lambda: ""

    def seconds_left(self) -> int | None:
        """What the page says; root decides, the portal only words it."""
        mode = _read_json(self.setup_mode)
        until = mode.get("until")
        if isinstance(until, bool) or not isinstance(until, (int, float)):
            return None
        left = int(until - self.uptime())
        return left if left > 0 else None

    def networks_list(self) -> list[dict[str, Any]]:
        listed = _read_json(self.networks).get("networks")
        out = []
        for entry in listed if isinstance(listed, list) else []:
            if isinstance(entry, dict) and isinstance(entry.get("ssid"), str):
                out.append({"ssid": entry["ssid"], "open": entry.get("open") is True})
        return out[:30]

    def write_request(self, payload: dict[str, Any]) -> None:
        """Atomically, mode 0600, never through a link: root's `apply` takes it from here."""
        tmp = self.request.with_name(f".request.{secrets.token_hex(4)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
        try:
            os.write(fd, json.dumps(payload).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.request)


def fetch_fingerprint(url: str = LAN_INFO_URL) -> Callable[[], str]:
    """Nox's certificate authority fingerprint, from its own listener on loopback (a public fact
    it shows anyone who asks), cached for a minute."""
    cache: dict[str, Any] = {"at": 0.0, "value": ""}

    def get() -> str:
        if time.monotonic() - cache["at"] < 60:
            return str(cache["value"])
        context = ssl.create_default_context()
        context.check_hostname = False
        # Loopback only, and only a public fact is read: the portal (user nox-setup) cannot read
        # Nox's CA file itself (/var/lib/nox is the service user's 0700 folder).
        context.verify_mode = ssl.CERT_NONE
        value = ""
        with contextlib.suppress(OSError, ValueError):
            with urllib.request.urlopen(url, timeout=3, context=context) as answer:  # noqa: S310
                data = json.loads(answer.read(16 * 1024))
            if isinstance(data, dict) and re.fullmatch(
                r"[0-9A-Fa-f:]{16,200}", str(data.get("ca_fingerprint", ""))
            ):
                value = str(data["ca_fingerprint"])
        cache.update(at=time.monotonic(), value=value)
        return value

    return get


# ---- protection ----------------------------------------------------------------------------


class Limiter:
    """Sliding one-minute window, per address and in total."""

    def __init__(
        self,
        per_address: int,
        total: int,
        clock: Callable[[], float] = time.monotonic,
        window_s: float = 60.0,
    ) -> None:
        self._per, self._total, self._clock, self._window = per_address, total, clock, window_s
        self._by: dict[str, deque[float]] = {}
        self._all: deque[float] = deque()
        self._lock = threading.Lock()

    def allow(self, address: str) -> bool:
        with self._lock:
            now = self._clock()
            for hits in (self._all, *self._by.values()):
                while hits and hits[0] <= now - self._window:
                    hits.popleft()
            for key in [k for k, v in self._by.items() if not v]:
                del self._by[key]
            hits = self._by.setdefault(address, deque())
            if len(hits) >= self._per or len(self._all) >= self._total:
                return False
            hits.append(now)
            self._all.append(now)
            return True


class Tokens:
    """The form's token: the time it was made and an HMAC of it with the phone's address. A page
    elsewhere cannot read it, another phone cannot reuse it, and it expires."""

    def __init__(self, key: bytes | None = None, clock: Callable[[], float] = time.time) -> None:
        self._key = key or secrets.token_bytes(32)
        self._clock = clock

    def _mac(self, address: str, issued: int) -> str:
        message = f"{address}|{issued}".encode()
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()[:32]

    def make(self, address: str) -> str:
        issued = int(self._clock())
        return f"{issued}.{self._mac(address, issued)}"

    def check(self, address: str, token: str) -> bool:
        issued_text, _, mac = token.partition(".")
        if not issued_text.isdigit() or len(mac) != 32:
            return False
        issued = int(issued_text)
        age = self._clock() - issued
        return 0 <= age <= TOKEN_TTL_S and hmac.compare_digest(mac, self._mac(address, issued))


# ---- the form ------------------------------------------------------------------------------


@dataclass
class Form:
    claim_code: str = field(default="", repr=False)
    network: str = ""
    other_ssid: str = ""
    psk: str = field(default="", repr=False)
    pin: str = field(default="", repr=False)
    pin2: str = field(default="", repr=False)
    passphrase: str = field(default="", repr=False)
    passphrase2: str = field(default="", repr=False)


def parse_form(body: bytes) -> tuple[Form, str, str]:
    """(form, token, language) from an urlencoded body; anything unexpected is dropped."""
    try:
        fields = urllib.parse.parse_qs(
            body.decode("utf-8"), keep_blank_values=True, strict_parsing=False, max_num_fields=20
        )
    except (UnicodeDecodeError, ValueError):
        return Form(), "", "en"

    def one(name: str, limit: int = 300) -> str:
        values = fields.get(name) or [""]
        return values[0][:limit]

    form = Form(
        claim_code=one("claim_code", 40),
        network=one("network", 64),
        other_ssid=one("other_ssid", 64),
        psk=one("psk", 80),
        pin=one("pin", MAX_PIN + 1),
        pin2=one("pin2", MAX_PIN + 1),
        passphrase=one("passphrase", MAX_PASSPHRASE + 1),
        passphrase2=one("passphrase2", MAX_PASSPHRASE + 1),
    )
    lang = one("lang", 4)
    return form, one("token", 80), lang if lang in STRINGS else "en"


def _ssid_ok(ssid: str) -> bool:
    raw = ssid.encode("utf-8")
    return 1 <= len(raw) <= 32 and not any(ord(c) < 0x20 or ord(c) == 0x7F for c in ssid)


def _psk_ok(psk: str) -> bool:
    if re.fullmatch(r"[0-9a-fA-F]{64}", psk):
        return True
    return 8 <= len(psk) <= 63 and all(0x20 <= ord(c) <= 0x7E for c in psk)


def check_form(
    form: Form, networks: list[dict[str, Any]], security_done: bool
) -> tuple[dict[str, str], dict[str, Any] | None]:
    """({field: error code}, the request for root) - the request only when there is no error."""
    errors: dict[str, str] = {}
    code = re.sub(r"[\s-]", "", form.claim_code).upper()
    if not 8 <= len(code) <= 32 or not code.isalnum() or not code.isascii():
        errors["claim_code"] = "claim_code"
    known = {n["ssid"]: n for n in networks}
    wifi: dict[str, Any] | None = None
    if form.network == CABLE:
        wifi = None
    elif form.network == OTHER:
        if not _ssid_ok(form.other_ssid):
            errors["other_ssid"] = "ssid_invalid"
        else:
            wifi = {"ssid": form.other_ssid, "psk": form.psk or None, "hidden": True}
    elif form.network in known:
        wifi = {"ssid": form.network, "psk": form.psk or None, "hidden": False}
        if not form.psk and not known[form.network]["open"]:
            errors["psk"] = "psk_missing"
    else:
        errors["network"] = "network_missing"
    if wifi is not None and form.psk and not _psk_ok(form.psk):
        errors["psk"] = "psk_invalid"
    request: dict[str, Any] = {"v": 1, "wifi": wifi, "claim_code": form.claim_code.strip()}
    if not security_done:
        if len(form.pin) < MIN_PIN or len(form.pin) > MAX_PIN or not form.pin.isprintable():
            errors["pin"] = "pin_short"
        elif form.pin != form.pin2:
            errors["pin2"] = "pin_mismatch"
        if not MIN_PASSPHRASE <= len(form.passphrase) <= MAX_PASSPHRASE or any(
            c in form.passphrase for c in "\x00\r\n"
        ):
            errors["passphrase"] = "passphrase_short"
        elif form.passphrase != form.passphrase2:
            errors["passphrase2"] = "passphrase_mismatch"
        request.update(pin=form.pin, passphrase=form.passphrase)
    return errors, (None if errors else request)


# ---- pages ---------------------------------------------------------------------------------


def _e(text: str) -> str:
    return html.escape(text, quote=True)


def page(lang: str, title: str, body: str) -> str:
    return (
        f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{_e(title)}</title><link rel="stylesheet" href="/portal.css"></head>'
        f"<body><main>{body}</main></body></html>"
    )


def _field(
    name: str,
    label: str,
    kind: str,
    errors: dict[str, str],
    t: dict[str, str],
    *,
    hint: str = "",
    value: str = "",
    extra: str = "",
) -> str:
    described = []
    parts = [f'<label for="{name}">{_e(label)}']
    if hint:
        parts.append(f'<span class="hint" id="{name}-hint">{_e(hint)}</span>')
        described.append(f"{name}-hint")
    if name in errors:
        parts.append(f'<span class="error" id="{name}-error">{_e(t["err_" + errors[name]])}</span>')
        described.append(f"{name}-error")
    parts.append("</label>")
    attrs = f' aria-describedby="{" ".join(described)}"' if described else ""
    if name in errors:
        attrs += ' aria-invalid="true"'
    shown = f' value="{_e(value)}"' if value else ""
    parts.append(f'<input type="{kind}" id="{name}" name="{name}"{shown}{attrs}{extra}>')
    return "".join(parts)


def form_page(
    lang: str,
    token: str,
    *,
    networks: list[dict[str, Any]],
    status: Mapping[str, Any],
    seconds_left: int,
    errors: dict[str, str] | None = None,
    form: Form | None = None,
    device_code: str | None = None,
) -> str:
    t = STRINGS[lang]
    errors = errors or {}
    form = form or Form()
    other_lang = "de" if lang == "en" else "en"
    body = [
        f'<p class="lang"><a href="/?lang={other_lang}" lang="{other_lang}">'
        f"{_e(t['language'])}</a></p>",
        f"<h1>{_e(t['title'])}</h1>",
        f"<p>{_e(t['intro'])}</p>",
        f'<p class="muted">{_e(t["closes"].format(minutes=max(1, seconds_left // 60)))}</p>',
    ]
    notices = []
    if status.get("state") == "failed" and status.get("error") in (
        "wrong_password",
        "not_found",
        "wifi_failed",
    ):
        ssid = str(status.get("tried_ssid") or "")
        notices.append(t["err_" + str(status["error"])].format(ssid=ssid or "Wi-Fi"))
    provision = status.get("provision")
    if isinstance(provision, dict) and provision.get("imported") is False:
        notices.append(t["err_provision"].format(code=str(provision.get("error", ""))[:40]))
    error = status.get("error")
    if error in ("claim_wrong", "claim_wait"):
        notices.append(t["err_" + str(error)])
    elif isinstance(error, str) and error and status.get("state") != "failed":
        notices.append(t["err_saved"].format(code=error[:40]))
    if errors:
        items = "".join(
            f'<li><a href="#{name}">{_e(t["err_" + code])}</a></li>'
            for name, code in errors.items()
        )
        body.append(
            f'<div class="summary" role="alert" tabindex="-1" id="errors">'
            f"<p><strong>{_e(t['errors_title'])}</strong></p><ul>{items}</ul></div>"
        )
    for notice in notices:
        body.append(f'<p class="box error" role="status">{_e(notice)}</p>')
    body.append('<form method="post" action="/setup" novalidate>')
    body.append(f'<input type="hidden" name="token" value="{_e(token)}">')
    body.append(f'<input type="hidden" name="lang" value="{lang}">')
    body.append(f"<fieldset><legend>{_e(t['claim_legend'])}</legend>")
    body.append(
        _field(
            "claim_code",
            t["claim_code"],
            "text",
            errors,
            t,
            hint=t["claim_hint_device" if device_code else "claim_hint_card"],
            value=form.claim_code or (device_code or ""),
            extra=' autocomplete="off" autocapitalize="characters" spellcheck="false" '
            'maxlength="40" required',
        )
    )
    body.append("</fieldset>")
    hint = t["wifi_hint"] if networks else t["wifi_none_found"]
    invalid = ' aria-invalid="true"' if "network" in errors else ""
    body.append(
        f'<fieldset id="network" aria-describedby="network-hint"{invalid}>'
        f"<legend>{_e(t['wifi_legend'])}</legend>"
        f'<p class="hint" id="network-hint">{_e(hint)}</p>'
    )
    if "network" in errors:
        body.append(f'<p class="error">{_e(t["err_" + errors["network"]])}</p>')
    choices = [
        (n["ssid"], n["ssid"] + (f" ({t['open_network']})" if n["open"] else "")) for n in networks
    ]
    choices += [(OTHER, t["other"]), (CABLE, t["cable"])]
    for index, (value, label) in enumerate(choices):
        checked = " checked" if form.network == value else ""
        body.append(
            f'<label class="choice" for="n{index}"><input type="radio" id="n{index}" '
            f'name="network" value="{_e(value)}"{checked}>{_e(label)}</label>'
        )
    body.append(
        _field(
            "other_ssid",
            t["other_ssid"],
            "text",
            errors,
            t,
            value=form.other_ssid,
            extra=' autocomplete="off" autocapitalize="none" spellcheck="false" maxlength="32"',
        )
    )
    body.append(
        _field(
            "psk",
            t["psk"],
            "password",
            errors,
            t,
            hint=t["psk_hint"],
            extra=' autocomplete="off" maxlength="64"',
        )
    )
    body.append("</fieldset>")
    body.append(f"<fieldset><legend>{_e(t['security_legend'])}</legend>")
    if status.get("security_done") is True:
        body.append(f"<p>{_e(t['security_done'])}</p>")
    else:
        body.append(
            _field(
                "pin",
                t["pin"],
                "password",
                errors,
                t,
                hint=t["pin_hint"],
                extra=' inputmode="numeric" autocomplete="new-password" '
                f'minlength="{MIN_PIN}" maxlength="{MAX_PIN}" required',
            )
        )
        body.append(
            _field(
                "pin2",
                t["pin2"],
                "password",
                errors,
                t,
                extra=' inputmode="numeric" autocomplete="new-password" required',
            )
        )
        body.append(
            _field(
                "passphrase",
                t["passphrase"],
                "password",
                errors,
                t,
                hint=t["passphrase_hint"],
                extra=f' autocomplete="new-password" minlength="{MIN_PASSPHRASE}" '
                f'maxlength="{MAX_PASSPHRASE}" required',
            )
        )
        body.append(
            _field(
                "passphrase2",
                t["passphrase2"],
                "password",
                errors,
                t,
                extra=' autocomplete="new-password" required',
            )
        )
    body.append("</fieldset>")
    body.append(f'<button type="submit">{_e(t["submit"])}</button></form>')
    return page(lang, t["title"], "".join(body))


def claim_url(code: str | None) -> str:
    base = f"https://nox.local:{DASHBOARD_PORT}/claim/"
    return base + (f"#code={urllib.parse.quote(code.replace('-', ''))}" if code else "")


def done_page(lang: str, *, ssid: str | None, code: str | None, fingerprint: str) -> str:
    t = STRINGS[lang]
    body = [f"<h1>{_e(t['done_title'])}</h1>"]
    body.append(f"<p>{_e(t['done_wifi'].format(ssid=ssid) if ssid else t['done_cable'])}</p>")
    body.append(f"<p>{_e(t['done_back'])}</p>")
    body.append(f"<p>{_e(t['done_open'])}</p>")
    url = claim_url(code)
    body.append(f'<p class="box mono"><a href="{_e(url)}">{_e(url.split("#")[0])}</a></p>')
    if code:
        body.append(f'<p>{_e(t["done_code"])}</p><p class="box code">{_e(code)}</p>')
    else:
        body.append(f"<p>{_e(t['done_card'])}</p>")
    if fingerprint:
        body.append(f'<p>{_e(t["done_fingerprint"])}</p><p class="box mono">{_e(fingerprint)}</p>')
    if ssid:
        body.append(f'<p class="muted">{_e(t["done_failed"])}</p>')
    body.append(f'<p class="muted">{_e(t["done_router"])}</p>')
    return page(lang, t["done_title"], "".join(body))


def message_page(lang: str, key: str) -> str:
    t = STRINGS[lang]
    return page(
        lang,
        t["title"],
        f'<h1>{_e(t["title"])}</h1><p role="alert">{_e(t[key])}</p>'
        f'<p><a href="/?lang={lang}">{_e(t["title"])}</a></p>',
    )


# ---- the server ----------------------------------------------------------------------------


@dataclass
class Portal:
    state: State
    address: str = ADDRESS
    port: int = 80
    tokens: Tokens = field(default_factory=Tokens)
    views: Limiter = field(default_factory=lambda: Limiter(per_address=120, total=600))
    posts: Limiter = field(default_factory=lambda: Limiter(per_address=6, total=30))

    @property
    def origin(self) -> str:
        return f"http://{self.address}" + ("" if self.port == 80 else f":{self.port}")


def make_handler(portal: Portal) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "nox-setup"
        sys_version = ""
        timeout = TIMEOUT_S
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return  # nothing a phone sends is logged (it carries passwords)

        def _send(
            self,
            code: int,
            body: bytes,
            content_type: str,
            lang: str = "en",
            headers: Mapping[str, str] | None = None,
        ) -> None:
            self.send_response(code)
            for key, value in {**HEADERS, **(headers or {})}.items():
                self.send_header(key, value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Language", lang)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            self.close_connection = True

        def _html(self, code: int, text: str, lang: str) -> None:
            self._send(code, text.encode("utf-8"), "text/html; charset=utf-8", lang)

        def _redirect(self) -> None:
            self._send(302, b"", "text/plain", headers={"Location": portal.origin + "/"})

        def _ours(self) -> bool:
            host = self.headers.get("Host", "").strip().lower()
            return host in (portal.address, f"{portal.address}:{portal.port}")

        def _peer(self) -> str:
            return str(self.client_address[0])

        def _lang(self, query: Mapping[str, list[str]]) -> str:
            return pick_language(query, self.headers.get("Accept-Language", ""))

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_GET(self) -> None:  # noqa: N802
            if not portal.views.allow(self._peer()):
                self._send(429, b"too many requests\n", "text/plain")
                return
            # Every name answers with this portal (captive DNS): send the phone here, so it
            # opens the portal by itself.
            if not self._ours():
                self._redirect()
                return
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query, max_num_fields=5)
            lang = self._lang(query)
            if parsed.path == "/portal.css":
                self._send(200, CSS.encode("utf-8"), "text/css; charset=utf-8")
                return
            if parsed.path != "/":
                self._redirect()
                return
            self._show_form(lang)

        def _show_form(
            self,
            lang: str,
            *,
            code: int = 200,
            errors: dict[str, str] | None = None,
            form: Form | None = None,
        ) -> None:
            left = portal.state.seconds_left()
            if left is None:
                self._html(410 if code == 200 else code, message_page(lang, "closed"), lang)
                return
            text = form_page(
                lang,
                portal.tokens.make(self._peer()),
                networks=portal.state.networks_list(),
                status=_read_json(portal.state.status),
                seconds_left=left,
                errors=errors,
                form=form,
                device_code=_read_text(portal.state.claim_code) or None,
            )
            self._html(code, text, lang)

        def do_POST(self) -> None:  # noqa: N802
            peer = self._peer()
            lang = self._lang({})
            if not portal.posts.allow(peer):
                self._html(429, message_page(lang, "rate_limited"), lang)
                return
            if not self._ours() or urllib.parse.urlsplit(self.path).path != "/setup":
                self._send(404, b"not found\n", "text/plain")
                return
            origin = self.headers.get("Origin")
            fetch_site = self.headers.get("Sec-Fetch-Site")
            content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            declared = self.headers.get("Content-Length", "")
            if (
                origin != portal.origin  # every browser sends it on a form POST
                or (fetch_site is not None and fetch_site not in ("same-origin", "none"))
                or content_type != "application/x-www-form-urlencoded"
                or not declared.isdigit()
                or int(declared) > MAX_BODY
            ):
                self._html(403, message_page(lang, "forbidden"), lang)
                return
            body = self.rfile.read(int(declared))
            form, token, lang = parse_form(body)
            if not portal.tokens.check(peer, token):
                self._html(403, message_page(lang, "forbidden"), lang)
                return
            if portal.state.seconds_left() is None:
                self._html(410, message_page(lang, "closed"), lang)
                return
            status = _read_json(portal.state.status)
            errors, request = check_form(
                form, portal.state.networks_list(), status.get("security_done") is True
            )
            if request is None:
                self._show_form(
                    lang,
                    code=400,
                    errors=errors,
                    form=Form(
                        claim_code=form.claim_code,
                        network=form.network,
                        other_ssid=form.other_ssid,
                    ),
                )
                return
            request["peer"] = peer
            try:
                portal.state.write_request(request)
            except OSError:
                self._html(503, message_page(lang, "rate_limited"), lang)
                return
            wifi = request.get("wifi")
            code = _read_text(portal.state.claim_code) or None
            if (
                code is None
                or code.replace("-", "") != form.claim_code.strip().replace("-", "").upper()
            ):
                code = None  # the card's code: never shown back
            self._html(
                200,
                done_page(
                    lang,
                    ssid=wifi["ssid"] if isinstance(wifi, dict) else None,
                    code=code,
                    fingerprint=portal.state.fingerprint(),
                ),
                lang,
            )

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler]) -> None:
        super().__init__(address, handler)
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            request.settimeout(TIMEOUT_S)
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def serve(portal: Portal) -> Server:
    return Server((portal.address, portal.port), make_handler(portal))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="nox_setup_portal.py")
    parser.add_argument("--bind", default=ADDRESS)
    parser.add_argument("--port", type=int, default=80)
    args = parser.parse_args(argv[1:])
    portal = Portal(state=State(fingerprint=fetch_fingerprint()), address=args.bind, port=args.port)
    try:
        server = serve(portal)
    except OSError as exc:
        print(f"nox-setup-portal: cannot listen on {args.bind}:{args.port}: {exc}", file=sys.stderr)
        return 1
    print(f"nox-setup-portal: listening on {portal.origin}", file=sys.stderr)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

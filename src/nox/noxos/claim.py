"""Claiming a Nox-OS device: the moment an unclaimed Raspberry Pi gets its owner (step O3).

A freshly flashed Nox-OS has nobody it trusts. Until it is claimed, its network listener serves
the claim and nothing else (`nox.lan.app.ClaimSurface`): the dashboard, pairing, nodes and the
WebSocket are refused, and the firewall keeps the node port closed (`nox_firewall.py`).

**Who may claim.** Whoever knows the one-time claim code, during the setup window: at most 30
minutes after the Pi was powered on (a power cycle opens a new one - the physical act that proves
someone is at the device). The code is on the setup card the PC assistant printed (only its hash
is on the device), or - without a card - made at every boot and shown on a screen plugged into
the Pi and in the setup hotspot's portal (`os/pi-gen/stage-nox/04-firstboot/files/`). The root
helper checks it (`claim`), slows down the address of wrong ones, and alone marks the device claimed
(`/var/lib/nox-os-claim/claimed`, root's): neither this process nor its user can mark it.

**What a claim does**, in one request (`POST /api/claim`, over the listener's TLS): pairs the
claiming browser as the first device (its token goes to that browser only, exactly as
`POST /api/pair` would), asks the helper to verify the code - which also sets the maintenance
passphrase sent and ends setup mode - sets the security PIN sent, and audits it. PIN and
passphrase come only from the claim page, never from the setup portal (plain HTTP over the
hotspot). A refused claim removes the device it had paired. Wrong codes slow the sender down,
root-side (`claim_wait`). After a claim the guard lets everything through, setup mode is over
and the firewall opens the node port. The claim page then says that this browser holds the only
key and offers to pair a second device right away.

**Reclaim** (physical access is full access): a `nox-reclaim` file on the boot partition makes
root forget the owner at the next boot and leave a note (`/var/lib/nox-os-claim/reclaim`, an id).
`apply_reclaim` - when Nox starts, before its listener - revokes every paired device, node and
their tokens and certificates, clears the security PIN, and remembers the id so it happens once.
Everything else Nox knows stays.

**State.** `ClaimGate.required()` is checked on every request: on Nox-OS and without root's mark,
the device is unclaimed - also when the mark cannot be read (fail closed). Once seen, "claimed"
is remembered for the life of the process; it never goes back.

Requests, for the UI roles: `noxos.claim.status` (read-only; whether there is an owner and the
setup window's time left) - O4's onboarding starts from it.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import ipaddress
import json
import re
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from nox.core.logging import get_logger
from nox.lan.app import ClaimSurface, PairLimiter
from nox.lan.devices import CLIENT_PREFIX, LanDevices
from nox.lan.tls import TlsMaterial
from nox.noxos.helper import OS_RELEASE, HelperCall, HelperError
from nox.security.model import AuditLog
from nox.security.secrets import PinManager

log = get_logger(__name__)

__all__ = [
    "CLAIMED_MARKER",
    "ClaimBody",
    "ClaimGate",
    "ClaimService",
    "ONBOARDING_URL",
    "STRINGS",
    "normalize_claim_code",
]

CLAIMED_MARKER = Path("/var/lib/nox-os-claim/claimed")
#: Where the claim page sends the new owner: O4's onboarding wizard starts there.
ONBOARDING_URL = "/dashboard/#onboarding"
#: The dashboard's devices page, where a second device is paired.
PAIR_URL = "/dashboard/#/devices/devices"
#: Root's note that the owner was forgotten (nox_setup.py reclaim), and what Nox did about it.
RECLAIM_MARK = Path("/var/lib/nox-os-claim/reclaim")
RECLAIM_DONE = "noxos-reclaim.json"
#: The claim page's own policy: its files, its API, nothing else, never framed.
CLAIM_PAGE_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)
CLAIM_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
MIN_PIN = 6
MAX_PIN = 64
MIN_PASSPHRASE = 12
MAX_PASSPHRASE = 256
#: Claim attempts per address per minute, and for all addresses together.
CLAIMS_PER_ADDRESS = 5
CLAIMS_TOTAL = 20
#: The helper's refusals -> (HTTP status, the page's error code).
REFUSALS: dict[str, tuple[int, str]] = {
    "claim_wrong": (401, "wrong_code"),
    "claim_wait": (429, "wait"),
    "claim_locked_lan": (429, "locked"),
    "claim_locked_hotspot": (429, "locked"),
    "not_setup": (403, "window_closed"),
    "claimed": (409, "claimed"),
    "no_claim_code": (503, "no_code"),
    "passphrase_short": (400, "passphrase_short"),
}


def normalize_claim_code(text: str) -> str | None:
    """The root side's rule (nox_setup.py, nox_os_helper.py): spaces and dashes do not count,
    case does not matter, I/L/O read as 1/1/0. None for anything that cannot be a claim code."""
    code = re.sub(r"[\s-]", "", text).upper().translate(str.maketrans("ILO", "110"))
    if not 8 <= len(code) <= 32 or any(c not in CLAIM_ALPHABET for c in code):
        return None
    return code


class ClaimGate:
    """Whether this device still waits for its owner. Inactive (never required) anywhere but on
    Nox-OS: a PC or a Pi set up with the installer has no claim."""

    def __init__(
        self,
        *,
        release: Path = OS_RELEASE,
        marker: Path = CLAIMED_MARKER,
        root_uid: int = 0,
    ) -> None:
        self._release = release
        self._marker = marker
        self._root_uid = root_uid
        self._claimed = False

    @property
    def nox_os(self) -> bool:
        return self._release.is_file()

    def claimed(self) -> bool:
        """Root's mark is there: a plain file that belongs to root. Remembered once seen."""
        if self._claimed:
            return True
        try:
            info = self._marker.lstat()
        except OSError:
            return False
        if stat.S_ISREG(info.st_mode) and info.st_uid == self._root_uid:
            self._claimed = True
        return self._claimed

    def required(self) -> bool:
        return self.nox_os and not self.claimed()

    def mark_claimed(self) -> None:
        """The helper answered "claimed": remember it at once, also before the file is seen."""
        self._claimed = True


class ClaimBody(BaseModel):
    code: str = Field(min_length=8, max_length=40, repr=False)
    #: Typed on the claiming device; empty means "use my guess".
    name: str = Field(default="", max_length=64)
    default_name: str = Field(default="", max_length=64)
    #: Needed only when the setup portal did not take one (`needs_pin`).
    pin: str | None = Field(default=None, max_length=MAX_PIN, repr=False)
    #: Needed only when none is set yet (`needs_passphrase`).
    passphrase: str | None = Field(default=None, max_length=MAX_PASSPHRASE, repr=False)
    #: The setup portal's one-time ticket (from the claim link): the claim counts on its side.
    ticket: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]{16,64}$")


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class ClaimService:
    def __init__(
        self,
        *,
        gate: ClaimGate,
        devices: LanDevices,
        helper: HelperCall,
        pin: PinManager,
        audit: AuditLog | None = None,
        tls: Callable[[], TlsMaterial | None] = lambda: None,
        limiter: PairLimiter | None = None,
    ) -> None:
        self.gate = gate
        self._devices = devices
        self._helper = helper
        self._pin = pin
        self._audit = audit
        self._tls = tls
        self._limiter = limiter or PairLimiter(per_address=CLAIMS_PER_ADDRESS, total=CLAIMS_TOTAL)
        self._lock = asyncio.Lock()

    def surface(self) -> ClaimSurface:
        return ClaimSurface(
            required=self.gate.required,
            status=self.status,
            claim=self.claim,
            asset=asset,
            policy=CLAIM_PAGE_CSP,
        )

    def _fingerprints(self) -> dict[str, str]:
        material = self._tls()
        if material is None:
            return {"fingerprint": "", "ca_fingerprint": ""}
        return {"fingerprint": material.server_fingerprint, "ca_fingerprint": material.fingerprint}

    async def _helper_status(self) -> dict[str, Any] | None:
        try:
            return await self._helper(["status"])
        except HelperError as exc:
            log.warning("noxos.claim_helper_failed", refused=exc.refused, code=exc.code)
            return None

    async def status(self) -> dict[str, Any]:
        """`GET /api/claim`: public facts - whether there is an owner, how long the window is
        open, which secrets the claim still needs. Never a code or a secret."""
        if not self.gate.required():
            return {"claimed": True}
        answer = await self._helper_status()
        if answer is None:
            return {"claimed": False, "available": False, **self._fingerprints()}
        if answer.get("claimed") is True:
            self.gate.mark_claimed()
            return {"claimed": True}
        left = answer.get("setup_seconds_left")
        return {
            "claimed": False,
            "available": True,
            "window_open": answer.get("setup_mode") is True,
            "seconds_left": left if isinstance(left, int) else None,
            "code_on_card": answer.get("claim_code") == "card",
            "needs_pin": not self._pin.is_set(),
            "needs_passphrase": answer.get("passphrase") is not True,
            "min_pin": MIN_PIN,
            "min_passphrase": MIN_PASSPHRASE,
            **self._fingerprints(),
        }

    def _audit_claim(self, actor: str, decision: str, result: str, **details: str) -> None:
        if self._audit is None:
            return
        self._audit.append(
            actor=actor,
            tool="noxos",
            action="noxos.claim",
            target="owner",
            decision=decision,
            result=result,
            details=details or None,
        )

    async def claim(self, raw: bytes, peer: str) -> tuple[int, dict[str, Any]]:
        """`POST /api/claim` -> (HTTP status, JSON). Same-origin and size were checked by the
        listener; the code, the window and the count of wrong codes by the root helper."""
        if not self.gate.required():
            return 409, {"error": "claimed"}
        if not self._limiter.allow(peer):
            log.warning("noxos.claim_rate_limited", peer=peer)
            return 429, {"error": "rate_limited"}
        try:
            body = ClaimBody.model_validate(json.loads(raw))
        except (ValidationError, ValueError, RecursionError):
            return 400, {"error": "invalid"}
        code = normalize_claim_code(body.code)
        if code is None:
            return 401, {"error": "wrong_code"}
        async with self._lock:
            return await self._claim(body, code, peer)

    async def _claim(self, body: ClaimBody, code: str, peer: str) -> tuple[int, dict[str, Any]]:
        state = await self._helper_status()
        if state is None:
            return 503, {"error": "unavailable"}
        if state.get("claimed") is True:
            self.gate.mark_claimed()
            return 409, {"error": "claimed"}
        if state.get("setup_mode") is not True:
            return 403, {"error": "window_closed"}
        needs_pin = not self._pin.is_set()
        if body.pin == "":
            body.pin = None
        if body.passphrase == "":
            body.passphrase = None
        pin_ok = body.pin is not None and MIN_PIN <= len(body.pin) <= MAX_PIN
        if (needs_pin or body.pin is not None) and not (
            pin_ok and body.pin is not None and body.pin.isprintable()
        ):
            return 400, {"error": "pin_required"}
        needs_passphrase = state.get("passphrase") is not True
        if (needs_passphrase or body.passphrase is not None) and (
            body.passphrase is None
            or len(body.passphrase) < MIN_PASSPHRASE
            or any(c in body.passphrase for c in "\x00\r\n")
        ):
            return 400, {"error": "passphrase_required"}
        # The owner's device first - its token reaches nobody until the helper said yes.
        start = await self._devices.start(name=body.name)
        redeemed = await self._devices.redeem(
            start.code, name=body.name, fallback_name=body.default_name
        )
        result = redeemed.result
        if not result.ok or redeemed.token is None:
            log.error("noxos.claim_pairing_failed", reason=result.reason)
            return 503, {"error": "unavailable"}
        actor = f"{CLIENT_PREFIX}{result.device_id}"
        lines = [f"claim {result.device_id} {_address(peer)}", f"code {_b64(code)}"]
        if body.passphrase is not None:
            lines.append(f"new {_b64(body.passphrase)}")
        if body.ticket is not None:
            lines.append(f"ticket {_b64(body.ticket)}")
        try:
            await self._helper(lines)
        except HelperError as exc:
            await self._devices.revoke(result.device_id, reason="claim_refused")
            unknown = (503, "unavailable")
            status, error = REFUSALS.get(exc.code, unknown) if exc.refused else unknown
            if error == "claimed":
                self.gate.mark_claimed()
            self._audit_claim(actor, "deny", error)
            log.warning("noxos.claim_refused", reason=error, peer=peer)
            return status, {"error": error}
        self.gate.mark_claimed()
        pin = body.pin
        pin_set = False
        if pin and not self._pin.is_set():
            try:
                await asyncio.to_thread(self._pin.set_pin, pin, by=actor)
                pin_set = True
            except ValueError:
                log.error("noxos.claim_pin_refused")
        self._audit_claim(actor, "allow", "ok", pin_set=str(pin_set).lower())
        log.warning("noxos.claimed", device_id=result.device_id, peer=peer)
        return 201, {
            "device_id": result.device_id,
            "client_id": actor,
            "name": result.name,
            "token": redeemed.token,
            "next": ONBOARDING_URL,
            # This browser holds the only key: the page offers to pair a second device at once.
            "pair_url": PAIR_URL,
        }


def _address(peer: str) -> str:
    """The peer's address for the helper's slow-down; an unknown one shares one key."""
    try:
        return str(ipaddress.ip_address(peer))
    except ValueError:
        return "0.0.0.0"  # noqa: S104 - a key, not a bind address


def apply_reclaim(
    db: Any,
    pin: PinManager,
    data_dir: Path,
    *,
    mark: Path = RECLAIM_MARK,
    root_uid: int = 0,
    audit: AuditLog | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int | None:
    """The owner was forgotten (root's reclaim note, a new id): revoke every paired device and
    node - their tokens and certificates - and clear the security PIN. Once per id; None when
    there is nothing to do, else how many pairings were revoked."""
    try:
        info = mark.lstat()
        note = json.loads(mark.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != root_uid or not isinstance(note, dict):
        return None
    ident = note.get("id")
    if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{32}", ident):
        return None
    done = data_dir / RECLAIM_DONE
    try:
        if json.loads(done.read_text("utf-8")).get("id") == ident:
            return None
    except (OSError, ValueError, AttributeError):
        pass
    from nox.node.repo import NodeRepository  # noqa: PLC0415 - only a reclaim needs it
    from nox.remote.repo import RemoteRepository  # noqa: PLC0415

    remote, nodes, now = RemoteRepository(db), NodeRepository(db), clock()
    revoked = 0
    for device in remote.list_devices(include_revoked=False):
        if remote.revoke_device(device.id, reason="reclaim", when=now):
            revoked += 1
        nodes.revoke_certs(device.id, reason="reclaim", when=now)
    if pin.is_set():
        pin.clear_pin(by="reclaim")
    done.write_text(json.dumps({"id": ident, "at": now.isoformat()}) + "\n", "utf-8")
    if audit is not None:
        audit.append(
            actor="root",
            tool="noxos",
            action="noxos.reclaim",
            target="owner",
            decision="allow",
            result="ok",
            details={"revoked": str(revoked)},
        )
    log.warning("noxos.reclaimed", revoked=revoked)
    return revoked


# ---- the claim page (claim_page/) ----------------------------------------------------------
#
# A page of its own, not the dashboard: an unclaimed device serves nothing else. No inline
# script or style (the listener's Content-Security-Policy), nothing from elsewhere, German and
# English (the texts below, put into claim.js), every field labelled, errors announced and linked.

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "title": "Claim this Nox",
        "intro": "This Nox has no owner yet. Enter its claim code to become its owner - this "
        "phone or computer is then signed in.",
        "loading": "Asking Nox ...",
        "unavailable": "Nox cannot answer right now. Try again in a minute.",
        "closed": "Setup is closed. Unplug the Pi's power and plug it in again - then you have "
        "30 minutes.",
        "minutes": "Setup is open for about {minutes} more minutes.",
        "code": "Claim code",
        "code_hint_card": "It is on your setup card.",
        "code_hint_device": "It is shown on a screen plugged into the Pi, and in the setup Wi-Fi.",
        "name": "Name of this device (optional)",
        "security": "Protect your Nox",
        "pin": "Security PIN",
        "pin_hint": "At least 6 characters. Nox asks for it before anything important.",
        "pin2": "Security PIN again",
        "passphrase": "Maintenance passphrase",
        "passphrase_hint": "At least 12 characters, for remote maintenance (SSH). Keep it safe.",
        "passphrase2": "Maintenance passphrase again",
        "only_key": "This browser is now the only key to your Nox. Pair a second device now - "
        "a phone or a computer - so that losing this one does not lock you out.",
        "pair_now": "Pair a second device",
        "continue": "Continue",
        "err_wait": "Wrong claim codes slow Nox down. Wait a moment, then try again.",
        "err_locked": "Too many wrong claim codes from the home network. Unplug the Pi's power "
        "and plug it in again - or use the link from the setup Wi-Fi.",
        "fingerprint": "Your browser warned because Nox made its own certificate. It is Nox if "
        "this fingerprint is the one shown on the Pi's screen or in the setup Wi-Fi:",
        "submit": "Claim Nox",
        "working": "Claiming ...",
        "errors": "Please check:",
        "done": "Done - you are the owner. Opening Nox ...",
        "not_saved": "This browser cannot keep the sign-in; you will be asked again.",
        "err_code": "Type the claim code.",
        "err_pin_short": "The PIN needs at least 6 characters.",
        "err_pin_mismatch": "The two PINs are not the same.",
        "err_passphrase_short": "The passphrase needs at least 12 characters.",
        "err_passphrase_mismatch": "The two passphrases are not the same.",
        "err_wrong_code": "That claim code is not right.",
        "err_window_closed": "Setup is closed. Unplug the Pi's power and plug it in again.",
        "err_claimed": "This Nox already has an owner.",
        "err_rate_limited": "Too many attempts. Wait a minute.",
        "err_pin_required": "Set a security PIN.",
        "err_passphrase_required": "Set a maintenance passphrase.",
        "err_origin": "Open this page from its own address.",
        "err_no_code": "This Nox has no claim code. Unplug its power and plug it in again.",
        "err_invalid": "Something in the form is not right.",
        "err_unavailable": "Nox cannot answer right now. Try again in a minute.",
        "err_network": "No connection to Nox. Is this device in the same network?",
        "language": "Deutsch",
    },
    "de": {
        "title": "Diesen Nox übernehmen",
        "intro": "Dieser Nox hat noch keinen Besitzer. Gib seinen Besitzer-Code ein, um sein "
        "Besitzer zu werden - dieses Handy oder dieser Computer ist dann angemeldet.",
        "loading": "Frage Nox ...",
        "unavailable": "Nox kann gerade nicht antworten. Versuch es in einer Minute noch einmal.",
        "closed": "Die Einrichtung ist geschlossen. Zieh kurz den Strom vom Pi und steck ihn "
        "wieder ein - dann hast du 30 Minuten.",
        "minutes": "Die Einrichtung ist noch etwa {minutes} Minuten offen.",
        "code": "Besitzer-Code",
        "code_hint_card": "Er steht auf deiner Einrichtungskarte.",
        "code_hint_device": "Er steht auf einem Bildschirm am Pi und im Einrichtungs-WLAN.",
        "name": "Name dieses Geräts (optional)",
        "security": "Nox schützen",
        "pin": "Sicherheits-PIN",
        "pin_hint": "Mindestens 6 Zeichen. Nox fragt danach, bevor es etwas Wichtiges tut.",
        "pin2": "Sicherheits-PIN wiederholen",
        "passphrase": "Wartungs-Passphrase",
        "passphrase_hint": "Mindestens 12 Zeichen, für die Fernwartung (SSH). Bewahr sie sicher "
        "auf.",
        "passphrase2": "Wartungs-Passphrase wiederholen",
        "only_key": "Dieser Browser ist jetzt der einzige Schlüssel zu deinem Nox. Kopple jetzt "
        "ein zweites Gerät - ein Handy oder einen Computer -, damit du nicht ausgesperrt bist, "
        "wenn dieses verloren geht.",
        "pair_now": "Zweites Gerät koppeln",
        "continue": "Weiter",
        "err_wait": "Falsche Besitzer-Codes bremsen Nox. Warte kurz und versuch es dann noch "
        "einmal.",
        "err_locked": "Zu viele falsche Besitzer-Codes aus dem Heimnetz. Zieh kurz den Strom vom "
        "Pi und steck ihn wieder ein - oder nimm den Link aus dem Einrichtungs-WLAN.",
        "fingerprint": "Dein Browser hat gewarnt, weil Nox sein eigenes Zertifikat gemacht hat. "
        "Es ist Nox, wenn dieser Fingerabdruck der vom Bildschirm am Pi oder aus dem "
        "Einrichtungs-WLAN ist:",
        "submit": "Nox übernehmen",
        "working": "Übernehme ...",
        "errors": "Bitte prüfen:",
        "done": "Fertig - du bist der Besitzer. Nox wird geöffnet ...",
        "not_saved": "Dieser Browser kann die Anmeldung nicht behalten; du wirst wieder gefragt.",
        "err_code": "Gib den Besitzer-Code ein.",
        "err_pin_short": "Die PIN braucht mindestens 6 Zeichen.",
        "err_pin_mismatch": "Die beiden PINs sind nicht gleich.",
        "err_passphrase_short": "Die Passphrase braucht mindestens 12 Zeichen.",
        "err_passphrase_mismatch": "Die beiden Passphrasen sind nicht gleich.",
        "err_wrong_code": "Dieser Besitzer-Code stimmt nicht.",
        "err_window_closed": "Die Einrichtung ist geschlossen. Zieh kurz den Strom vom Pi und "
        "steck ihn wieder ein.",
        "err_claimed": "Dieser Nox hat schon einen Besitzer.",
        "err_rate_limited": "Zu viele Versuche. Warte eine Minute.",
        "err_pin_required": "Lege eine Sicherheits-PIN fest.",
        "err_passphrase_required": "Lege eine Wartungs-Passphrase fest.",
        "err_origin": "Öffne diese Seite über ihre eigene Adresse.",
        "err_no_code": "Dieser Nox hat keinen Besitzer-Code. Zieh kurz den Strom und steck ihn "
        "wieder ein.",
        "err_invalid": "Etwas im Formular stimmt nicht.",
        "err_unavailable": "Nox kann gerade nicht antworten. Versuch es in einer Minute noch "
        "einmal.",
        "err_network": "Keine Verbindung zu Nox. Ist dieses Gerät im selben Netz?",
        "language": "English",
    },
}

_PAGE = resources.files("nox.noxos") / "claim_page"


@functools.cache
def _assets() -> dict[str, tuple[bytes, str]]:
    page = (_PAGE / "index.html").read_bytes()
    script = (_PAGE / "claim.js").read_text("utf-8")
    strings = json.dumps(STRINGS, ensure_ascii=True)
    return {
        "": (page, "text/html; charset=utf-8"),
        "index.html": (page, "text/html; charset=utf-8"),
        "claim.css": ((_PAGE / "claim.css").read_bytes(), "text/css; charset=utf-8"),
        "claim.js": (
            script.replace("__STRINGS__", strings).encode("utf-8"),
            "text/javascript; charset=utf-8",
        ),
    }


def asset(name: str) -> tuple[bytes, str] | None:
    """`/claim/<name>`: the page, its style and its script (the texts are STRINGS)."""
    return _assets().get(name)

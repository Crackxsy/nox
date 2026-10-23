"""Nox-OS root helper: the only way the unprivileged Nox service changes the system (os/README.md).

Socket-activated (`nox-os-helper.socket`, one `nox-os-helper@.service` per connection), it reads
one request and answers with one JSON line. It knows exactly these requests and nothing else - no
shell, no paths, no units, no options:

    status                        on/off, until when, the stored keys' fingerprints, whether a
                                  maintenance passphrase is set or locked
    keys set                      then `pass <passphrase>` and 1-8 public keys, one per line
                                  (ed25519 or ECDSA, optionally security-key backed; no options,
                                  no other types): stored root-owned, answered with fingerprints
    keys clear                    forget the stored keys
    ssh on <minutes> <fp>[,<fp>]  then `pass <passphrase>`: on for 5-240 minutes with the stored
                                  keys - only if their fingerprints are exactly the confirmed ones
    ssh off
    maint set                     then `new <passphrase>`: only while /run/nox-os/setup-mode exists
                                  (root's; the first-boot setup creates it) and none is set yet
                                  - or root created /var/lib/nox-os/reset-maintenance
    maint change                  then `pass <old>` and `new <passphrase>`
    claim <device id> <address>   then `code <claim code>`, optionally `new <passphrase>` and
                                  `ticket <ticket>` (step O3, from Nox's claim page): only in
                                  setup mode, only while unclaimed. Marks the device claimed
                                  (/var/lib/nox-os-claim/claimed), sets the maintenance passphrase
                                  (the one sent, else the one kept from the setup portal), ends
                                  setup mode and answers with the kept security PIN (once)
    stage <address>               then `code <claim code>` and optionally `pin <PIN>`,
                                  `new <passphrase>` and `ticket <ticket>` (root only: the setup
                                  portal's request, via nox_setup.py): checks the claim code and
                                  only then keeps PIN, passphrase and the ticket's hash for the
                                  claim (/run/nox-os/setup/pending.json, root 0600). Nothing is
                                  applied before the claim

**Wrong claim codes** are counted on two independent sides, `lan` (Nox's claim page) and
`hotspot` (the setup portal). Per side and per source - an IPv4 address, an IPv6 /64 - each wrong
code makes that source wait (1 s, doubling, at most 5 minutes); a waiting source is never
forgotten, and when a side's table of 256 sources is full of waiting ones, the whole side waits 5
minutes (fail closed). Per side and boot, 200 wrong codes lock that side until the next boot
(`claim_locked_lan`, `claim_locked_hotspot`); the other side is unaffected. A right code (compared
in constant time) succeeds unless its source waits or its side is locked. A claim that carries the
ticket the setup portal issued with the right code counts on the hotspot side - the owner who
joined the hotspot (its WPA2 password is on the Pi's screen, the setup card or the LED, never on
the LAN) finishes the claim even when someone in the home network locked the LAN side.

Passphrases and claim codes travel base64-encoded (UTF-8); passphrases have at least 12
characters.

**Setup mode** is root's file /run/nox-os/setup-mode (nox_setup.py writes it at boot while the
device is unclaimed): it counts only while it is this boot's and its end - seconds since boot,
CLOCK_BOOTTIME - is not reached, at most 30 minutes after power-on. The same rule as nox_setup.py
and the firewall.

**The maintenance passphrase** is the barrier between code running as the service user and root:
the security PIN is checked by Nox, whose files that user can read - a short PIN's hash there can
be guessed offline. The passphrase is checked here only: scrypt, root 0600 in
/var/lib/nox-os/maintenance, never in a file the service user can read, never logged. Five wrong
ones lock it for 15 minutes, every further five for twice as long (at most a day), counted in
/var/lib/nox-os/maintenance-lockout. Without a passphrase, `keys set` and `ssh on` are refused.

The keys are this helper's alone (/var/lib/nox-os/ssh_keys, root 0600). Nothing the service user
can write is ever read as a key: a planted file in Nox's folder grants nothing.

Who may ask: the socket is `root:nox-os-helper 0660` in a folder only root and that group may
enter, and only nox.service carries the group (`SupplementaryGroups=`) - the user `nox` itself is
not in it. The peer must then be root, or the main process of nox.service: the service user's
uid, nox.service's MainPID, in nox.service's cgroup, and the same process from the first check to
the last (its pidfd where the kernel gives one, SO_PEERPIDFD; else its start time). Commands Nox
runs for someone run as that user and may inherit the group, but are never the main process; the
core makes itself non-dumpable on Nox-OS, so they cannot trace it either. The core asks only after
the security PIN and an administrator's request (`nox.noxos`), and audits it.

`ssh on` writes the keys under /run/nox-os, starts `nox-ssh.service`, and only once sshd runs a
time-limited sudo rule for `nox-admin` (all gone at the next boot). `nox-ssh.service` is sshd with
its own configuration: key only, `nox-admin` only, from private networks only - never from this
device itself, the firewall drops that. It then opens port 22 for private networks and starts a
timer that switches all of it off again - open sessions included. `ssh off` does that at once.
`ssh.service` itself stays masked throughout. A request that finds the state file saying "on"
while sshd is not running cleans up first.

Also run by systemd without a socket: `--expire` (the timer) and `--stopped` (`ExecStopPost=` of
nox-ssh.service: whatever stopped sshd, the keys, the sudo rule and port 22 go with it).

Standard library only (`python3 -I -S`).
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import hmac
import json
import os
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

SERVICE_USER = "nox"
SERVICE_UNIT = "nox.service"
#: The leaf the command sandbox moves the core into (`nox.sandbox._cgroup`).
CORE_LEAF = "nox-core"
ADMIN_USER = "nox-admin"
RUN = Path("/run/nox-os")
STATE = RUN / "ssh.json"
KEYS = RUN / "ssh" / "authorized_keys"
SUDOERS = RUN / "sudoers.d" / "nox-admin"
#: The keys the owner pasted, kept for the next switch-on: root's, on the card, 0600.
STORED_KEYS = Path("/var/lib/nox-os/ssh_keys")
#: The maintenance passphrase (scrypt) and its lockout: root's, 0600.
PASSPHRASE = Path("/var/lib/nox-os/maintenance")
LOCKOUT = Path("/var/lib/nox-os/maintenance-lockout")
#: Serialises every check of the passphrase across helper instances (flock).
PASSPHRASE_LOCK = RUN / "helper" / "passphrase.lock"
#: Root's deliberate reset: with it (and in setup mode) `maint set` may replace a passphrase.
RESET_FLAG = Path("/var/lib/nox-os/reset-maintenance")
#: Present (root's) only while the first-boot setup runs: the one time a passphrase may be set
#: without knowing an old one, and the claim's window (step O3).
SETUP_MODE = RUN / "setup-mode"
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
#: The claim code's hash (nox_setup.py; from the setup card or made at boot), root 0600.
CLAIM = Path("/var/lib/nox-os/claim.json")
#: Root's mark that the device has an owner: Nox and the firewall read it (0644).
CLAIMED = Path("/var/lib/nox-os-claim/claimed")
#: Wrong claim codes this boot, per source (in RAM: a power cycle starts again).
CLAIM_FAILURES = RUN / "claim-failures"
#: PIN and passphrase the setup portal took with the right claim code - kept until the claim.
PENDING = RUN / "setup" / "pending.json"
CLAIM_LOCK = RUN / "helper" / "claim.lock"
CLAIM_PREFIX = b"nox-claim-v1:"
CLAIM_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
#: Wrong claim codes per side (lan, hotspot) and boot before that side is locked until reboot.
CLAIM_BUDGET = 200
#: Per source: the first wrong code costs 1 s, each further one twice as much, at most 5 min.
CLAIM_BACKOFF_S = 1.0
CLAIM_BACKOFF_MAX_S = 300.0
#: Sources remembered per side; a waiting one is never dropped - a full table makes the side wait.
CLAIM_SOURCES = 256
CLAIM_SIDES = ("lan", "hotspot")
MAX_TICKET = 64
MIN_PIN = 6
MAX_PIN = 64
SETUP_CLOSE_UNIT = "nox-setup-close.service"
MIN_PASSPHRASE = 12
MAX_PASSPHRASE = 256
SCRYPT = {"n": 2**15, "r": 8, "p": 1}
SCRYPT_MAXMEM = 64 * 1024 * 1024
LOCK_AFTER = 5
LOCK_SECONDS = 15 * 60
LOCK_MAX_SECONDS = 24 * 3600
SSH_UNIT = "nox-ssh.service"
FIREWALL_UNIT = "nox-firewall.service"
OFF_TIMER_PREFIX = "nox-ssh-off-"
HELPER = "/usr/local/lib/nox-os/nox_os_helper.py"
#: getsockopt option for a pidfd of the peer (Linux 6.5+); Python may not name it yet.
SO_PEERPIDFD = getattr(socket, "SO_PEERPIDFD", 77)

MIN_MINUTES = 5
MAX_MINUTES = 240
MAX_KEYS = 8
MAX_REQUEST_BYTES = 16 * 1024
MAX_KEY_LINE = 1024
MAX_COMMENT = 100

#: Key types accepted, and the curve each ECDSA type must name inside its blob.
KEY_TYPES: dict[str, str | None] = {
    "ssh-ed25519": None,
    "sk-ssh-ed25519@openssh.com": None,
    "ecdsa-sha2-nistp256": "nistp256",
    "ecdsa-sha2-nistp384": "nistp384",
    "ecdsa-sha2-nistp521": "nistp521",
    "sk-ecdsa-sha2-nistp256@openssh.com": "nistp256",
}
_BASE64 = re.compile(r"[A-Za-z0-9+/]+={0,2}")
_COMMENT = re.compile(r"[\x21-\x7e]+(?: [\x21-\x7e]+)*")


class Refused(ValueError):
    """A request the helper does not carry out; the message is safe to show, `code` is stable."""

    def __init__(self, message: str, code: str = "refused") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PublicKey:
    type: str
    blob: bytes
    comment: str = ""

    @property
    def line(self) -> str:
        data = base64.b64encode(self.blob).decode("ascii")
        return f"{self.type} {data}" + (f" {self.comment}" if self.comment else "")

    @property
    def fingerprint(self) -> str:
        digest = base64.b64encode(hashlib.sha256(self.blob).digest()).decode("ascii")
        return "SHA256:" + digest.rstrip("=")


def _strings(blob: bytes, count: int) -> list[bytes]:
    """The first `count` SSH wire-format strings of `blob` (uint32 length + bytes)."""
    out, pos = [], 0
    for _ in range(count):
        if pos + 4 > len(blob):
            raise Refused("the key is damaged")
        (length,) = struct.unpack(">I", blob[pos : pos + 4])
        pos += 4
        if length > len(blob) - pos:
            raise Refused("the key is damaged")
        out.append(blob[pos : pos + length])
        pos += length
    return out


def parse_key(line: str) -> PublicKey:
    """One public key as `ssh-keygen` prints it: `<type> <base64> [comment]`. No options in front
    (`command=`, `from=` ...), no other key types, nothing that is not printable ASCII."""
    text = line.strip()
    if not text or len(text) > MAX_KEY_LINE:
        raise Refused("a public key is one line of at most 1024 characters")
    if any(not (32 <= ord(c) < 127) for c in text):
        raise Refused("a public key is plain ASCII on one line")
    parts = text.split(None, 2)
    if len(parts) < 2 or parts[0] not in KEY_TYPES:
        raise Refused("only ed25519 and ECDSA public keys are accepted (ssh-ed25519 ...)")
    kind, data = parts[0], parts[1]
    comment = parts[2].strip() if len(parts) == 3 else ""
    if not _BASE64.fullmatch(data):
        raise Refused("the key is damaged")
    try:
        blob = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise Refused("the key is damaged") from None
    curve = KEY_TYPES[kind]
    fields = _strings(blob, 2)
    if fields[0] != kind.encode("ascii"):
        raise Refused("the key's type does not match its contents")
    if curve is None and len(fields[1]) != 32:
        raise Refused("the key is damaged")
    if curve is not None and fields[1] != curve.encode("ascii"):
        raise Refused("the key's curve does not match its type")
    if comment and (len(comment) > MAX_COMMENT or not _COMMENT.fullmatch(comment)):
        raise Refused("the key's comment is too long or has characters it may not have")
    return PublicKey(kind, blob, comment)


@dataclass(frozen=True)
class Request:
    command: str  # status | off | keys_set | keys_clear | on | maint_set | maint_change | claim
    minutes: int = 0
    keys: tuple[PublicKey, ...] = field(default_factory=tuple)
    #: For "on": the fingerprints the owner confirmed - they must be exactly the stored keys.
    fingerprints: tuple[str, ...] = field(default_factory=tuple)
    #: The maintenance passphrase given (`pass`), and a new one (`new`). Never logged.
    passphrase: str | None = field(default=None, repr=False)
    new_passphrase: str | None = field(default=None, repr=False)
    #: For "claim": Nox's id for the owner's device; for "claim" and "stage": who sent it and the
    #: claim code typed, and for "stage" the PIN to keep.
    device: str = ""
    address: str = ""
    code: str | None = field(default=None, repr=False)
    pin: str | None = field(default=None, repr=False)
    #: The setup portal's one-time ticket (stage: to keep; claim: to present).
    ticket: str | None = field(default=None, repr=False)


_FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")


def _secret(line: str, label: str) -> str:
    """`<label> <base64 of UTF-8>` -> the passphrase. Its value never reaches a message."""
    prefix, sep, data = line.partition(" ")
    if prefix != label or not sep or not _BASE64.fullmatch(data):
        raise Refused(
            f"the request needs `{label}` with the maintenance passphrase", "passphrase_missing"
        )
    try:
        text = base64.b64decode(data, validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        raise Refused("the maintenance passphrase is not readable", "passphrase_wrong") from None
    if not 1 <= len(text) <= MAX_PASSPHRASE or any(c in text for c in "\x00\n\r"):
        raise Refused("the maintenance passphrase is not readable", "passphrase_wrong")
    return text


def _coded(line: str, label: str) -> str:
    """`<label> <base64 of UTF-8>` for a claim code (refused as a wrong code otherwise)."""
    prefix, sep, data = line.partition(" ")
    if prefix != label or not sep or not _BASE64.fullmatch(data) or len(data) > 128:
        raise Refused("the request needs the claim code", "claim_wrong")
    try:
        return base64.b64decode(data, validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        raise Refused("the claim code is not readable", "claim_wrong") from None


def _labelled(lines: list[str], allowed: set[str]) -> dict[str, str]:
    """Optional `<label> <value>` lines, each label at most once, no others."""
    out = {line.partition(" ")[0]: line for line in lines}
    if len(out) != len(lines) or set(out) - allowed:
        raise Refused("unknown request")
    return out


def _ticket(line: str | None) -> str | None:
    if line is None:
        return None
    ticket = _coded(line, "ticket")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", ticket):
        raise Refused("the ticket is not readable", "claim_wrong")
    return ticket


def _code_line(line: str) -> str:
    code = normalize_claim_code(_coded(line, "code"))
    if code is None:
        raise Refused("that is not a claim code", "claim_wrong")
    return code


def _address(text: str) -> str:
    """The sender's address as the caller saw it (its key for the slow-down), checked."""
    import ipaddress  # noqa: PLC0415 - only the claim needs it

    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        raise Refused("the request needs the sender's address") from None


def normalize_claim_code(text: str) -> str | None:
    """The same rule as nox_setup.py: no spaces or dashes, any case, I/L/O read as 1/1/0."""
    code = re.sub(r"[\s-]", "", text).upper().translate(str.maketrans("ILO", "110"))
    if not 8 <= len(code) <= 32 or any(c not in CLAIM_ALPHABET for c in code):
        return None
    return code


def claim_hash(code: str) -> str:
    return "sha256:" + hashlib.sha256(CLAIM_PREFIX + code.encode("ascii")).hexdigest()


def _new_secret(line: str) -> str:
    text = _secret(line, "new")
    if len(text) < MIN_PASSPHRASE:
        raise Refused(
            f"a maintenance passphrase has at least {MIN_PASSPHRASE} characters", "passphrase_short"
        )
    return text


def parse_request(data: bytes) -> Request:
    """The request on the socket. Anything else is refused before anything happens."""
    if len(data) > MAX_REQUEST_BYTES:
        raise Refused("request too large")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise Refused("unknown request") from None
    lines = text.split("\n")
    words = lines[0].split(" ")
    rest = [line for line in lines[1:] if line.strip()]
    if words == ["status"] and not rest:
        return Request("status")
    if words == ["ssh", "off"] and not rest:
        return Request("off")
    if words == ["keys", "clear"] and not rest:
        return Request("keys_clear")
    if words == ["maint", "set"] and len(rest) == 1:
        return Request("maint_set", new_passphrase=_new_secret(rest[0]))
    if (
        len(words) == 3
        and words[0] == "claim"
        and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", words[1])
        and 1 <= len(rest) <= 3
    ):
        extra = _labelled(rest[1:], {"new", "ticket"})
        return Request(
            "claim",
            device=words[1],
            address=_address(words[2]),
            code=_code_line(rest[0]),
            new_passphrase=_new_secret(extra["new"]) if "new" in extra else None,
            ticket=_ticket(extra.get("ticket")),
        )
    if len(words) == 2 and words[0] == "stage" and 1 <= len(rest) <= 4:
        extra = _labelled(rest[1:], {"pin", "new", "ticket"})
        pin = None
        if "pin" in extra:
            pin = _coded(extra["pin"], "pin")
            if not MIN_PIN <= len(pin) <= MAX_PIN or not pin.isprintable():
                raise Refused(f"a security PIN has {MIN_PIN} to {MAX_PIN} characters", "pin_short")
        return Request(
            "stage",
            address=_address(words[1]),
            code=_code_line(rest[0]),
            pin=pin,
            new_passphrase=_new_secret(extra["new"]) if "new" in extra else None,
            ticket=_ticket(extra.get("ticket")),
        )
    if words == ["maint", "change"] and len(rest) == 2:
        return Request(
            "maint_change", passphrase=_secret(rest[0], "pass"), new_passphrase=_new_secret(rest[1])
        )
    if words == ["keys", "set"]:
        if not rest:
            raise Refused("the maintenance passphrase and at least one public key")
        passphrase = _secret(rest[0], "pass")
        key_lines = rest[1:]
        if not key_lines:
            raise Refused("at least one public key")
        if len(key_lines) > MAX_KEYS:
            raise Refused(f"at most {MAX_KEYS} public keys")
        keys: dict[bytes, PublicKey] = {}
        for line in key_lines:
            key = parse_key(line)
            keys.setdefault(key.blob, key)
        return Request("keys_set", keys=tuple(keys.values()), passphrase=passphrase)
    if (
        len(words) == 4
        and words[:2] == ["ssh", "on"]
        and re.fullmatch(r"[0-9]{1,4}", words[2])
        and len(rest) == 1
    ):
        minutes = int(words[2])
        if not MIN_MINUTES <= minutes <= MAX_MINUTES:
            raise Refused(f"between {MIN_MINUTES} and {MAX_MINUTES} minutes")
        confirmed = words[3].split(",")
        if not 1 <= len(confirmed) <= MAX_KEYS or not all(
            _FINGERPRINT.fullmatch(f) for f in confirmed
        ):
            raise Refused("the confirmed key fingerprints are missing or malformed")
        return Request(
            "on", minutes, fingerprints=tuple(confirmed), passphrase=_secret(rest[0], "pass")
        )
    raise Refused("unknown request")


# ---- the system ----------------------------------------------------------------------------


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False, timeout=60)  # noqa: S603


def _uid_of(name: str) -> int | None:
    import pwd  # noqa: PLC0415 - Linux only; the rest of this file is checked on every system

    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError:
        return None


def _uptime() -> float:
    return time.clock_gettime(time.CLOCK_BOOTTIME)  # type: ignore[attr-defined,unused-ignore]


@dataclass
class System:
    """Everything the helper reads and changes, in one place (tests replace it)."""

    run: Callable[..., subprocess.CompletedProcess[str]] = _run
    clock: Callable[[], float] = time.time
    root: Path = Path("/")
    uid_of: Callable[[str], int | None] = _uid_of
    #: Who owns what only root may create (tests run as someone else).
    root_uid: int = 0
    #: Seconds since boot (CLOCK_BOOTTIME): the setup window's clock, which no clock change moves.
    uptime: Callable[[], float] = _uptime

    def path(self, path: Path) -> Path:
        return self.root / path.relative_to("/")

    def write(self, path: Path, text: str, mode: int) -> None:
        target = self.path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.tmp")
        fd = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), mode
        )
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, target)

    def read(self, path: Path) -> str:
        return self.path(path).read_text("ascii", errors="replace")

    def remove(self, path: Path) -> None:
        try:
            self.path(path).unlink()
        except FileNotFoundError:
            pass

    def read_state(self) -> dict[str, object]:
        try:
            state = json.loads(self.path(STATE).read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return state if isinstance(state, dict) else {}

    def systemctl(self, *args: str) -> bool:
        return self.run("systemctl", *args).returncode == 0

    def active(self, unit: str) -> bool:
        return self.run("systemctl", "is-active", "--quiet", unit).returncode == 0

    def alive(self, pidfd: int) -> bool:
        """Whether the process behind `pidfd` still runs (signal 0 tests, sends nothing)."""
        try:
            signal.pidfd_send_signal(pidfd, 0)  # type: ignore[attr-defined,unused-ignore]
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # it exists; this helper may just not signal it
        return True


# ---- the stored keys: root's, never the service's ------------------------------------------


def stored_keys(system: System) -> list[PublicKey]:
    """The keys the owner last pasted, as only this helper stores them (root, 0600). A key file
    anywhere the service user can write is never read."""
    try:
        text = system.read(STORED_KEYS)
    except OSError:
        return []
    keys = []
    for line in text.splitlines():
        if line.strip():
            try:
                keys.append(parse_key(line))
            except Refused:
                continue
    return keys[:MAX_KEYS]


def _key_views(keys: Sequence[PublicKey]) -> list[dict[str, str]]:
    return [{"type": k.type, "fingerprint": k.fingerprint, "comment": k.comment} for k in keys]


def status(system: System) -> dict[str, object]:
    state = system.read_state()
    running = system.active(SSH_UNIT)
    until = state.get("until")
    on = running and state.get("on") is True and isinstance(until, (int, float))
    return {
        "ok": True,
        "ssh": "on" if on else "off",
        "until": int(until) if on and isinstance(until, (int, float)) else None,
        "active": state.get("keys", []) if on else [],
        "keys": _key_views(stored_keys(system)),
        "running": running,
        "passphrase": passphrase_set(system),
        "locked_until": locked_until(system),
        "setup_mode": in_setup_mode(system),
        "setup_seconds_left": setup_seconds_left(system),
        "claimed": claimed(system),
        "claim_code": claim_source(system),
        **_staged(system),
    }


def set_keys(system: System, keys: Sequence[PublicKey]) -> dict[str, object]:
    """Replace the stored keys; the answer names their fingerprints for the owner to check."""
    system.write(STORED_KEYS, "".join(f"{key.line}\n" for key in keys), 0o600)
    return status(system)


def clear_keys(system: System) -> dict[str, object]:
    system.remove(STORED_KEYS)
    return status(system)


def switch_off(system: System, reason: str) -> dict[str, object]:
    """Everything back: timer, sshd (open sessions too), keys, sudo rule, port 22. Idempotent."""
    system.systemctl("stop", f"{OFF_TIMER_PREFIX}*.timer")
    system.systemctl("stop", SSH_UNIT)
    clean_up(system, reason)
    return status(system)


def clean_up(system: System, reason: str) -> None:
    system.remove(KEYS)
    system.remove(SUDOERS)
    # Sessions live in logind's scopes, not in sshd's unit: end them by name. No session, no harm.
    system.run("loginctl", "terminate-user", ADMIN_USER)
    system.write(
        STATE,
        json.dumps({"on": False, "since": int(system.clock()), "reason": reason}) + "\n",
        0o644,
    )
    system.systemctl("restart", FIREWALL_UNIT)


def switch_on(system: System, minutes: int, confirmed: Sequence[str]) -> dict[str, object]:
    keys = stored_keys(system)
    if not keys:
        raise Refused("no public key stored; paste one first")
    if sorted(k.fingerprint for k in keys) != sorted(set(confirmed)):
        raise Refused("the stored keys are not the ones confirmed; check the fingerprints again")
    now = system.clock()
    until = int(now + minutes * 60)
    system.write(KEYS, "".join(f"{key.line}\n" for key in keys), 0o644)
    system.write(
        STATE,
        json.dumps({"on": True, "until": until, "keys": [k.fingerprint for k in keys]}) + "\n",
        0o644,
    )
    if not system.systemctl("start", SSH_UNIT) or not system.active(SSH_UNIT):
        switch_off(system, "failed")
        raise Refused(f"remote maintenance could not be switched on (start {SSH_UNIT})")
    # Administration over SSH needs root; nox-admin has no password, so sudo asks for none - and
    # only while sshd runs for remote maintenance (the rule lives under /run and goes with it).
    system.write(SUDOERS, f"{ADMIN_USER} ALL=(ALL:ALL) NOPASSWD: ALL\n", 0o440)
    if not system.systemctl("restart", FIREWALL_UNIT):
        switch_off(system, "failed")
        raise Refused(f"remote maintenance could not be switched on (restart {FIREWALL_UNIT})")
    # The way back: a timer of its own per switch-on. An earlier one that fires first finds a
    # later end in the state file and does nothing (`expire`).
    timer = system.run(
        "systemd-run",
        "--quiet",
        f"--unit={OFF_TIMER_PREFIX}{int(now)}",
        f"--on-active={minutes}min",
        "--timer-property=AccuracySec=10s",
        "/usr/bin/python3",
        "-I",
        "-S",
        HELPER,
        "--expire",
    )
    if timer.returncode != 0:
        switch_off(system, "failed")
        raise Refused("remote maintenance could not be switched on (no off timer)")
    return status(system)


def expire(system: System) -> None:
    """The off timer fired: switch off unless a later switch-on moved the end."""
    until = system.read_state().get("until")
    if isinstance(until, (int, float)) and until > system.clock() + 30:
        return
    switch_off(system, "expired")


def reconcile(system: System) -> None:
    """The state file says "on" but sshd does not run (it crashed, or the machine lost power
    halfway): clean up before anything else, so nothing stays switched on without sshd."""
    if system.read_state().get("on") is True and not system.active(SSH_UNIT):
        clean_up(system, "reconciled")


def handle(request: Request, system: System, *, root: bool = True) -> dict[str, object]:
    """`root`: the peer is root (nox_setup.py), not Nox - only root may stage."""
    reconcile(system)
    if request.command == "status":
        return status(system)
    if request.command == "off":
        return switch_off(system, "dashboard")
    if request.command == "keys_clear":
        return clear_keys(system)
    if request.command == "claim":
        return claim(system, request)
    if request.command == "stage":
        if not root:
            raise Refused("only the setup portal stages", "not_allowed")
        return stage(system, request)
    # One request at a time touches the passphrase and its lockout, whatever runs in parallel.
    with passphrase_lock(system):
        if request.command == "maint_set":
            set_first_passphrase(system, request.new_passphrase or "")
            return status(system)
        # Everything else needs the maintenance passphrase.
        verify_passphrase(system, request.passphrase or "")
        if request.command == "maint_change":
            store_passphrase(system, request.new_passphrase or "")
            return status(system)
    if request.command == "keys_set":
        return set_keys(system, request.keys)
    return switch_on(system, request.minutes, request.fingerprints)


# ---- the maintenance passphrase ------------------------------------------------------------


def _root_flag(system: System, path: Path) -> bool:
    """`path` exists, is a plain file and belongs to root (only root can put it there)."""
    try:
        info = system.path(path).lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_uid == system.root_uid


def _boot_id(system: System) -> str:
    try:
        value = system.read(BOOT_ID).strip()
    except OSError:
        return ""
    return value if re.fullmatch(r"[0-9a-f-]{36}", value) else ""


def _setup_window(system: System) -> dict[str, object] | None:
    """Root's setup-mode file, this boot's, its end (seconds since boot) not reached."""
    if not _root_flag(system, SETUP_MODE):
        return None
    mode = _read_json(system, SETUP_MODE)
    until = mode.get("until")
    current = _boot_id(system)
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


def in_setup_mode(system: System) -> bool:
    return _setup_window(system) is not None


def setup_seconds_left(system: System) -> int | None:
    mode = _setup_window(system)
    until = mode.get("until") if mode else None
    return max(0, int(until - system.uptime())) if isinstance(until, (int, float)) else None


def claimed(system: System) -> bool:
    return _root_flag(system, CLAIMED)


def claim_source(system: System) -> str | None:
    """Where this device's claim code is: on the setup card, or made on the device (shown on its
    screen and in the setup portal) - never the code itself."""
    if claimed(system):
        return None
    source = _read_json(system, CLAIM).get("source")
    return source if source in ("card", "device") else None


def _staged(system: System) -> dict[str, bool]:
    pending = _read_json(system, PENDING) if _root_flag(system, PENDING) else {}
    return {
        "pending_pin": isinstance(pending.get("pin"), str),
        "pending_passphrase": isinstance(pending.get("passphrase"), str),
    }


def source_key(address: str) -> str:
    """Who is slowed down: an IPv4 address, or an IPv6 /64 (one home network's devices may change
    their IPv6 address at will within it)."""
    import ipaddress  # noqa: PLC0415 - only the claim needs it

    parsed = ipaddress.ip_address(address)
    if isinstance(parsed, ipaddress.IPv6Address):
        if parsed.ipv4_mapped is not None:
            return str(parsed.ipv4_mapped)
        return str(ipaddress.ip_network(f"{parsed}/64", strict=False))
    return str(parsed)


def _failures(system: System) -> dict[str, dict[str, object]]:
    record = _read_json(system, CLAIM_FAILURES)
    sides = record.get("sides") if record.get("boot_id") == _boot_id(system) else None
    out: dict[str, dict[str, object]] = {}
    for side in CLAIM_SIDES:
        entry = sides.get(side) if isinstance(sides, dict) else None
        if not isinstance(entry, dict) or not isinstance(entry.get("sources"), dict):
            entry = {"total": 0, "sources": {}, "full_until": 0.0}
        out[side] = entry
    return out


def _number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _may_try(system: System, side: str, key: str) -> None:
    """Refuse a locked side, a side whose table is full of waiting sources, a waiting source."""
    entry = _failures(system)[side]
    if _number(entry.get("total")) >= CLAIM_BUDGET:
        raise Refused(
            f"too many wrong claim codes on the {side} side; unplug the power and plug it in again",
            f"claim_locked_{side}",
        )
    now = system.uptime()
    sources = entry["sources"]
    assert isinstance(sources, dict)
    mine = sources.get(key)
    until = max(
        _number(entry.get("full_until")),
        _number(mine.get("until")) if isinstance(mine, dict) else 0.0,
    )
    if until > now:
        raise Refused(f"wait {int(until - now) + 1} s before the next claim code", "claim_wait")


def _failed(system: System, side: str, key: str) -> None:
    """One wrong code: the side's count, the source's wait. Waiting sources are kept; only
    sources whose wait is over may make room, and a table full of waiting ones makes the whole
    side wait (fail closed)."""
    sides = _failures(system)
    entry = sides[side]
    entry["total"] = int(_number(entry.get("total"))) + 1
    now = system.uptime()
    sources = entry["sources"]
    assert isinstance(sources, dict)
    # Room only from sources whose wait is over (their count is kept as long as there is room).
    over = [k for k, v in sources.items() if k != key and _number(v.get("until")) <= now]
    while key not in sources and len(sources) >= CLAIM_SOURCES and over:
        sources.pop(over.pop(0))
    mine = sources.get(key)
    count = int(_number(mine.get("count"))) + 1 if isinstance(mine, dict) else 1
    delay = min(CLAIM_BACKOFF_S * 2 ** (count - 1), CLAIM_BACKOFF_MAX_S)
    if key in sources or len(sources) < CLAIM_SOURCES:
        sources[key] = {"count": count, "until": now + delay}
    else:
        entry["full_until"] = now + CLAIM_BACKOFF_MAX_S
    record = {"boot_id": _boot_id(system), "sides": sides}
    system.write(CLAIM_FAILURES, json.dumps(record) + "\n", 0o600)


def _ticket_hash(ticket: str) -> str:
    return hashlib.sha256(b"nox-claim-ticket-v1:" + ticket.encode("ascii")).hexdigest()


def _check_code(system: System, request: Request, side: str, key: str) -> None:
    """Unclaimed, in the window, the side not locked and the source not waiting - then the code,
    compared in constant time. Every wrong one counts, whatever was staged."""
    if claimed(system):
        raise Refused("this Nox already has an owner", "claimed")
    if not in_setup_mode(system):
        raise Refused(
            "setup is closed; unplug the power and plug it in again to reopen it", "not_setup"
        )
    _may_try(system, side, key)
    expected = str(_read_json(system, CLAIM).get("hash", ""))
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
        raise Refused("this device has no claim code", "no_claim_code")
    if not hmac.compare_digest(claim_hash(request.code or ""), expected):
        _failed(system, side, key)
        raise Refused("wrong claim code", "claim_wrong")


def stage(system: System, request: Request) -> dict[str, object]:
    """The setup portal's PIN and passphrase - kept, never applied, and only with the right
    claim code: whoever reaches the portal first sets nothing without it. The claim applies
    them, in the same step that marks the device claimed. The portal's ticket is kept as a hash,
    with the source it came from: a claim presenting it counts on the hotspot side."""
    with exclusive(system, CLAIM_LOCK):
        key = source_key(request.address)
        _check_code(system, request, "hotspot", key)
        pending = _read_json(system, PENDING) if _root_flag(system, PENDING) else {}
        pending = {k: v for k, v in pending.items() if k in ("pin", "passphrase", "ticket")}
        if request.pin is not None:
            pending["pin"] = request.pin
        if request.new_passphrase is not None:
            pending["passphrase"] = request.new_passphrase
        if request.ticket is not None:
            pending["ticket"] = {"hash": _ticket_hash(request.ticket), "source": key}
        system.write(PENDING, json.dumps(pending) + "\n", 0o600)
    answer = status(system)
    answer["staged"] = "ok"
    return answer


def _claim_side(pending: dict[str, object], request: Request) -> tuple[str, str]:
    """(side, source key): the hotspot side with the portal's ticket, else the LAN."""
    ticket = pending.get("ticket")
    if request.ticket is not None and isinstance(ticket, dict):
        if hmac.compare_digest(str(ticket.get("hash", "")), _ticket_hash(request.ticket)):
            return "hotspot", str(ticket.get("source", ""))
    return "lan", source_key(request.address)


def claim(system: System, request: Request) -> dict[str, object]:
    """The claim (step O3): the code against its hash, in the setup window, while unclaimed.
    Success marks the device claimed - root's file, the firewall and Nox go by it - sets the
    maintenance passphrase (the one sent, else the one staged with the right claim code in the
    setup portal; never over an existing one), ends setup mode - hotspot and portal down before
    the firewall is regenerated - and hands over the staged PIN."""
    with exclusive(system, CLAIM_LOCK):
        pending = _read_json(system, PENDING) if _root_flag(system, PENDING) else {}
        side, key = _claim_side(pending, request)
        _check_code(system, request, side, key)
        passphrase = request.new_passphrase
        if passphrase is None and isinstance(pending.get("passphrase"), str):
            passphrase = str(pending["passphrase"])
        if passphrase is not None:
            with passphrase_lock(system):
                reset = _root_flag(system, RESET_FLAG)
                if not passphrase_set(system) or reset:
                    store_passphrase(system, passphrase)
                    system.remove(RESET_FLAG)
        pin = pending.get("pin") if isinstance(pending.get("pin"), str) else None
        marker = {"v": 1, "claimed_at": int(system.clock()), "owner_device": request.device}
        system.write(CLAIMED, json.dumps(marker) + "\n", 0o644)
        # The window ends with the claim, before anything else happens.
        system.remove(SETUP_MODE)
        system.remove(CLAIM)
        system.remove(PENDING)
        system.remove(CLAIM_FAILURES)
    # The hotspot and the portal go first (and wait for it), then the firewall opens the node
    # port: there is no moment with the hotspot up and the setup rules gone.
    system.systemctl("start", SETUP_CLOSE_UNIT)
    system.systemctl("restart", FIREWALL_UNIT)
    answer = status(system)
    answer["claim"] = "ok"
    answer["pin"] = base64.b64encode(str(pin).encode("utf-8")).decode("ascii") if pin else None
    return answer


@contextlib.contextmanager
def exclusive(system: System, lock: Path) -> Iterator[None]:
    """An exclusive lock (flock) on `lock` across helper instances - one process each."""
    path = system.path(lock)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            import fcntl  # noqa: PLC0415 - POSIX only; the tests on Windows run one at a time
        except ImportError:
            fcntl = None  # type: ignore[assignment]
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


def passphrase_lock(system: System) -> contextlib.AbstractContextManager[None]:
    """An exclusive lock held from reading the lockout state through the scrypt check to writing
    the new count: parallel guesses must not all read the same count and each add one."""
    return exclusive(system, PASSPHRASE_LOCK)


def set_first_passphrase(system: System, passphrase: str) -> None:
    """Only during setup, and only when none is set - or when root deliberately asked for a reset
    with a flag only root can create (/var/lib/nox-os/reset-maintenance: physical access to the
    card, or a running maintenance session). Never a way around the old passphrase for the
    service."""
    if not in_setup_mode(system):
        raise Refused(
            "a maintenance passphrase can be set only during setup; change it with the old one",
            "not_setup",
        )
    reset = _root_flag(system, RESET_FLAG)
    if passphrase_set(system) and not reset:
        raise Refused(
            "a maintenance passphrase is already set; change it with the old one",
            "passphrase_exists",
        )
    store_passphrase(system, passphrase)
    if reset:
        system.remove(RESET_FLAG)


def _scrypt(passphrase: str, salt: bytes, params: dict[str, int]) -> bytes:
    return hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=params["n"],
        r=params["r"],
        p=params["p"],
        maxmem=SCRYPT_MAXMEM,
        dklen=32,
    )


def store_passphrase(system: System, passphrase: str) -> None:
    if len(passphrase) < MIN_PASSPHRASE:
        raise Refused(
            f"a maintenance passphrase has at least {MIN_PASSPHRASE} characters",
            "passphrase_short",
        )
    salt = os.urandom(16)
    record = {
        "v": 1,
        "kdf": "scrypt",
        **SCRYPT,
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": base64.b64encode(_scrypt(passphrase, salt, SCRYPT)).decode("ascii"),
    }
    system.write(PASSPHRASE, json.dumps(record) + "\n", 0o600)
    system.remove(LOCKOUT)


def _read_json(system: System, path: Path) -> dict[str, object]:
    try:
        data = json.loads(system.read(path))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def passphrase_set(system: System) -> bool:
    return bool(_read_json(system, PASSPHRASE).get("hash"))


def locked_until(system: System) -> int | None:
    until = _read_json(system, LOCKOUT).get("until")
    if isinstance(until, (int, float)) and not isinstance(until, bool) and until > system.clock():
        return int(until)
    return None


def verify_passphrase(system: System, passphrase: str) -> None:
    """Pass, or raise Refused - a wrong one counts towards the lockout."""
    if locked_until(system) is not None:
        raise Refused(
            "too many wrong maintenance passphrases; try again later", "passphrase_locked"
        )
    record = _read_json(system, PASSPHRASE)
    try:
        salt = base64.b64decode(str(record["salt"]), validate=True)
        expected = base64.b64decode(str(record["hash"]), validate=True)
        params = {k: int(str(record[k])) for k in ("n", "r", "p")}
    except (KeyError, ValueError, binascii.Error):
        raise Refused(
            "no maintenance passphrase is set; set one during setup", "passphrase_missing"
        ) from None
    if hmac.compare_digest(_scrypt(passphrase, salt, params), expected):
        system.remove(LOCKOUT)
        return
    previous = _read_json(system, LOCKOUT).get("failures")
    failures = (previous if isinstance(previous, int) else 0) + 1
    entry: dict[str, object] = {"failures": failures}
    if failures % LOCK_AFTER == 0:
        seconds = min(LOCK_SECONDS * 2 ** (failures // LOCK_AFTER - 1), LOCK_MAX_SECONDS)
        entry["until"] = int(system.clock() + seconds)
    system.write(LOCKOUT, json.dumps(entry) + "\n", 0o600)
    raise Refused("wrong maintenance passphrase", "passphrase_wrong")


# ---- who is asking -------------------------------------------------------------------------


@dataclass(frozen=True)
class Peer:
    pid: int
    uid: int
    #: A pidfd of the connecting process (SO_PEERPIDFD, Linux 6.5+): it cannot be reused.
    pidfd: int | None = None


def read_peer(conn: socket.socket) -> Peer:
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, _gid = struct.unpack("3i", creds)
    try:
        raw = conn.getsockopt(socket.SOL_SOCKET, SO_PEERPIDFD, 4)
        pidfd: int | None = struct.unpack("i", raw)[0]
    except OSError:
        pidfd = None
    return Peer(pid, uid, pidfd if pidfd is not None and pidfd >= 0 else None)


def _show(system: System) -> dict[str, str]:
    shown = system.run(
        "systemctl", "show", "--property=MainPID", "--property=ControlGroup", SERVICE_UNIT
    )
    if shown.returncode != 0:
        return {}
    values = {}
    for line in shown.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key] = value.strip()
    return values


def _cgroup(system: System, pid: int) -> str | None:
    try:
        text = system.read(Path(f"/proc/{pid}/cgroup"))
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("0::"):
            return line[3:]
    return None


def _start_time(system: System, pid: int) -> str | None:
    """Field 22 of /proc/<pid>/stat: when the process started, in clock ticks since boot."""
    try:
        text = system.read(Path(f"/proc/{pid}/stat"))
    except OSError:
        return None
    _, sep, after = text.rpartition(")")
    fields = after.split()
    return fields[19] if sep and len(fields) > 19 else None


def _fdinfo_pid(system: System, pidfd: int) -> int | None:
    try:
        text = system.read(Path(f"/proc/self/fdinfo/{pidfd}"))
    except OSError:
        return None
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key == "Pid" and value.strip().lstrip("-").isdigit():
            return int(value.strip())
    return None


def peer_allowed(peer: Peer, system: System) -> bool:
    """Root, or the main process of nox.service as the service user - bound to that process,
    not to a number another process could reuse.

    The socket's group is the first boundary (only nox.service carries it). Then: the uid is the
    service user's, the pid is nox.service's MainPID, the process sits in nox.service's cgroup
    (its own or the `nox-core` leaf the sandbox moves it to), and it is still the same process
    after all of that - by its pidfd where the kernel gives one, else by its start time read
    before and after."""
    if peer.uid == 0:
        return True
    if peer.uid != system.uid_of(SERVICE_USER):
        return False
    started = _start_time(system, peer.pid)
    shown = _show(system)
    main, unit_cgroup = shown.get("MainPID", ""), shown.get("ControlGroup", "")
    if not main.isdigit() or int(main) <= 1 or int(main) != peer.pid or not unit_cgroup:
        return False
    if _cgroup(system, peer.pid) not in (unit_cgroup, f"{unit_cgroup}/{CORE_LEAF}"):
        return False
    if peer.pidfd is not None:
        if _fdinfo_pid(system, peer.pidfd) != peer.pid or not system.alive(peer.pidfd):
            return False
    return started is not None and _start_time(system, peer.pid) == started


def _read_request(conn: socket.socket) -> bytes:
    conn.settimeout(10)
    chunks, size = [], 0
    while size <= MAX_REQUEST_BYTES:
        chunk = conn.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def _describe(request: Request) -> str:
    if request.command == "on":
        return f"ssh on for {request.minutes} min, keys {list(request.fingerprints)}"
    if request.command == "keys_set":
        return f"keys set {[k.fingerprint for k in request.keys]}"
    if request.command == "claim":
        return f"claim for device {request.device} from {request.address}"
    if request.command == "stage":
        return f"stage from {request.address}"
    return request.command


def serve(conn: socket.socket, system: System) -> int:
    def answer(payload: dict[str, object]) -> None:
        conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))

    peer = read_peer(conn)
    try:
        allowed = peer_allowed(peer, system)
    finally:
        if peer.pidfd is not None:
            os.close(peer.pidfd)
    if not allowed:
        print(
            f"nox-os-helper: refused a request from pid {peer.pid} uid {peer.uid}", file=sys.stderr
        )
        answer({"ok": False, "error": "not allowed", "code": "not_allowed"})
        return 0
    try:
        request = parse_request(_read_request(conn))
        result = handle(request, system, root=peer.uid == 0)
    except Refused as exc:
        print(f"nox-os-helper: refused: {exc}", file=sys.stderr)
        answer({"ok": False, "error": str(exc), "code": exc.code})
        return 0  # a refusal is an answer, not a failure of the unit
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"nox-os-helper: failed: {type(exc).__name__}", file=sys.stderr)
        answer({"ok": False, "error": "the helper failed"})
        return 1
    print(f"nox-os-helper: {_describe(request)} (pid {peer.pid})", file=sys.stderr)
    answer(result)
    return 0


def main(argv: list[str]) -> int:
    system = System()
    if argv[1:] == ["--expire"]:
        expire(system)
        return 0
    if argv[1:] == ["--stopped"]:
        clean_up(system, "stopped")
        return 0
    if argv[1:]:
        print("usage: nox_os_helper.py [--expire | --stopped]  (socket on stdin)", file=sys.stderr)
        return 2
    conn = socket.socket(fileno=sys.stdin.fileno())
    try:
        return serve(conn, system)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

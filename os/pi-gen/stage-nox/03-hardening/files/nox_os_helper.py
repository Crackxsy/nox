"""Nox-OS root helper: the only way the unprivileged Nox service changes the system (os/README.md).

Socket-activated (`nox-os-helper.socket`, one `nox-os-helper@.service` per connection), it reads
one request and answers with one JSON line. It knows exactly these requests and nothing else - no
shell, no paths, no units, no options:

    status                        on/off, until when, the stored keys' fingerprints
    keys set                      followed by 1-8 public keys, one per line (ed25519 or ECDSA,
                                  optionally security-key backed; no options, no other types):
                                  stored root-owned, answered with their fingerprints
    keys clear                    forget the stored keys
    ssh on <minutes> <fp>[,<fp>]  on for 5-240 minutes with the stored keys - only if their
                                  fingerprints are exactly the ones the owner confirmed
    ssh off

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

`ssh on` writes the keys and a time-limited sudo rule for `nox-admin` under /run/nox-os (gone at
the next boot), starts `nox-ssh.service` (its own sshd configuration: key only, `nox-admin` only,
from private networks only - never from this device itself, the firewall drops that), opens port
22 for private networks and starts a timer that switches all of it off again - open sessions
included. `ssh off` does that at once. `ssh.service` itself stays masked throughout.

Also run by systemd without a socket: `--expire` (the timer) and `--stopped` (`ExecStopPost=` of
nox-ssh.service: whatever stopped sshd, the keys, the sudo rule and port 22 go with it).

Standard library only (`python3 -I -S`).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
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
    """A request the helper does not carry out; the message is safe to show."""


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
    command: str  # "status" | "off" | "keys_set" | "keys_clear" | "on"
    minutes: int = 0
    keys: tuple[PublicKey, ...] = field(default_factory=tuple)
    #: For "on": the fingerprints the owner confirmed - they must be exactly the stored keys.
    fingerprints: tuple[str, ...] = field(default_factory=tuple)


_FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")


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
    if words == ["keys", "set"]:
        if not rest:
            raise Refused("at least one public key")
        if len(rest) > MAX_KEYS:
            raise Refused(f"at most {MAX_KEYS} public keys")
        keys: dict[bytes, PublicKey] = {}
        for line in rest:
            key = parse_key(line)
            keys.setdefault(key.blob, key)
        return Request("keys_set", keys=tuple(keys.values()))
    if (
        len(words) == 4
        and words[:2] == ["ssh", "on"]
        and re.fullmatch(r"[0-9]{1,4}", words[2])
        and not rest
    ):
        minutes = int(words[2])
        if not MIN_MINUTES <= minutes <= MAX_MINUTES:
            raise Refused(f"between {MIN_MINUTES} and {MAX_MINUTES} minutes")
        confirmed = words[3].split(",")
        if not 1 <= len(confirmed) <= MAX_KEYS or not all(
            _FINGERPRINT.fullmatch(f) for f in confirmed
        ):
            raise Refused("the confirmed key fingerprints are missing or malformed")
        return Request("on", minutes, fingerprints=tuple(confirmed))
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


@dataclass
class System:
    """Everything the helper reads and changes, in one place (tests replace it)."""

    run: Callable[..., subprocess.CompletedProcess[str]] = _run
    clock: Callable[[], float] = time.time
    root: Path = Path("/")
    uid_of: Callable[[str], int | None] = _uid_of

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
    # Administration over SSH needs root; nox-admin has no password, so sudo asks for none - and
    # only while remote maintenance is on (the rule lives under /run and goes with it).
    system.write(SUDOERS, f"{ADMIN_USER} ALL=(ALL:ALL) NOPASSWD: ALL\n", 0o440)
    system.write(
        STATE,
        json.dumps({"on": True, "until": until, "keys": [k.fingerprint for k in keys]}) + "\n",
        0o644,
    )
    steps = (
        ("start", (SSH_UNIT,)),
        ("restart", (FIREWALL_UNIT,)),
    )
    for verb, units in steps:
        if not system.systemctl(verb, *units):
            switch_off(system, "failed")
            raise Refused(f"remote maintenance could not be switched on ({verb} {units[0]})")
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


def handle(request: Request, system: System) -> dict[str, object]:
    if request.command == "status":
        return status(system)
    if request.command == "off":
        return switch_off(system, "dashboard")
    if request.command == "keys_set":
        return set_keys(system, request.keys)
    if request.command == "keys_clear":
        return clear_keys(system)
    return switch_on(system, request.minutes, request.fingerprints)


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
        answer({"ok": False, "error": "not allowed"})
        return 1
    try:
        request = parse_request(_read_request(conn))
        result = handle(request, system)
    except Refused as exc:
        print(f"nox-os-helper: refused: {exc}", file=sys.stderr)
        answer({"ok": False, "error": str(exc)})
        return 1
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

"""The Nox-OS root helper (os/pi-gen/stage-nox/03-hardening/files/nox_os_helper.py): three
requests and nothing else (keys stored root-owned, switching on only with the
confirmed fingerprints), keys checked exactly as the core checks them, everything it switches on
it switches off again, and only root or nox.service's main process - that very process, not its
number - may ask."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from nox.noxos.keys import InvalidKeyError, parse_key
from tests.unit.nox_os.conftest import HARDENING, load_tool
from tests.unit.noxos.conftest import ECDSA, ED25519, ED25519_B, RSA, SK_ED25519

helper = load_tool("nox_os_helper", HARDENING)
FPB = parse_key(ED25519).fingerprint.encode()


class FakeRun:
    """systemctl and systemd-run as the helper calls them; `fail` makes one verb fail."""

    def __init__(self, fail: str = "") -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail = fail
        self.active: set[str] = set()

    def __call__(self, *args: str) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        code, out = 0, ""
        if args[:2] == ("systemctl", "is-active"):
            code = 0 if args[-1] in self.active else 3
        elif args[:2] == ("systemctl", "start"):
            self.active.add(args[2])
        elif args[:2] == ("systemctl", "stop"):
            self.active.discard(args[2])
        elif args[:2] == ("systemctl", "show"):
            out = "MainPID=4242\nControlGroup=/system.slice/nox.service\n"
        if self.fail and self.fail in " ".join(args):
            code = 1
        return subprocess.CompletedProcess(args, code, out, "")


def system(tmp_path: Path, run: FakeRun | None = None) -> Any:
    return helper.System(run=run or FakeRun(), clock=lambda: 1_000_000.0, root=tmp_path)


# ---- the requests ----------------------------------------------------------------------------


def test_the_requests() -> None:
    assert helper.parse_request(b"status\n").command == "status"
    assert helper.parse_request(b"ssh off").command == "off"
    assert helper.parse_request(b"keys clear").command == "keys_clear"
    request = helper.parse_request(f"keys set\n{ED25519}\n{ECDSA} admin@example.invalid\n".encode())
    assert (request.command, len(request.keys)) == ("keys_set", 2)
    fp = FPB.decode()
    request = helper.parse_request(f"ssh on 60 {fp},{fp}".encode())
    assert (request.command, request.minutes, request.fingerprints) == ("on", 60, (fp, fp))


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"STATUS",
        b"status extra",
        b"status\nssh-ed25519 AAAA",
        b"ssh on",
        b"ssh on 60",  # no confirmed fingerprints
        b"ssh on 60\n" + ED25519.encode(),  # keys come with `keys set`, never with `on`
        b"ssh on 4 " + FPB,
        b"ssh on 241 " + FPB,
        b"ssh on -5 " + FPB,
        b"ssh on 60 " + FPB + b" extra",
        b"ssh  on 60 " + FPB,
        b"ssh on 60 SHA256:short",
        b"ssh on 60 " + b",".join([FPB] * 9),
        b"ssh on 60 " + FPB + b"\n" + ED25519.encode(),
        b"shell rm -rf /",
        b"keys set",
        b"keys set\n" + RSA.encode(),
        b"keys set\n" + b"\n".join([ED25519.encode()] * 9),
        b"keys set\n" + b'command="id" ' + ED25519.encode(),
        "keys set\nssh-ed25519 AAAAé".encode(),
        b"keys clear\n" + ED25519.encode(),
        b"ssh off\n" + ED25519.encode(),
        b"x" * (17 * 1024),
    ],
)
def test_everything_else_is_refused(data: bytes) -> None:
    with pytest.raises(helper.Refused):
        helper.parse_request(data)


CORPUS = [
    ED25519,
    ED25519_B + " admin@example.invalid",
    ECDSA,
    SK_ED25519 + " security key",
    RSA,
    "ssh-dss AAAA",
    'from="192.0.2.1" ' + ED25519,
    ED25519 + " ä",
    ED25519 + " " + "c" * 101,
    ED25519.replace("ssh-ed25519", "ecdsa-sha2-nistp256"),
    "ecdsa-sha2-nistp384 " + ECDSA.split()[1],
    ED25519[:-4],
    "",
    "   ",
    "ssh-ed25519",
    ED25519 + "\r",
]


@pytest.mark.parametrize("line", CORPUS)
def test_the_helper_and_the_core_agree_on_every_key(line: str) -> None:
    try:
        core = parse_key(line).line
    except InvalidKeyError:
        core = None
    try:
        root = helper.parse_key(line).line
    except helper.Refused:
        root = None
    assert core == root


def test_a_key_line_is_rewritten_canonically() -> None:
    key = helper.parse_key(f"  {ED25519}   admin@example.invalid  ")
    assert key.line == f"{ED25519} admin@example.invalid"
    assert key.fingerprint.startswith("SHA256:") and "=" not in key.fingerprint


# ---- switching ---------------------------------------------------------------------------

#: The sudo rule is written 0440, which Windows turns into a read-only file nobody may delete.
posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
FP = parse_key(ED25519).fingerprint


def stored(sys_: Any, *lines: str) -> None:
    keys = helper.parse_request(("keys set\n" + "\n".join(lines)).encode()).keys
    helper.set_keys(sys_, keys)


def test_keys_are_stored_root_only_and_answered_with_their_fingerprints(tmp_path: Path) -> None:
    sys_ = system(tmp_path)
    request = helper.parse_request(f"keys set\n{ED25519} laptop\n{ECDSA}\n".encode())
    answer = helper.handle(request, sys_)
    assert [(k["fingerprint"], k["comment"]) for k in answer["keys"]] == [
        (FP, "laptop"),
        (parse_key(ECDSA).fingerprint, ""),
    ]
    path = tmp_path / "var/lib/nox-os/ssh_keys"
    assert path.read_text("ascii") == f"{ED25519} laptop\n{ECDSA}\n"
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o777 == 0o600
    assert answer["ssh"] == "off"  # storing keys switches nothing on
    helper.handle(helper.parse_request(b"keys clear"), sys_)
    assert not path.exists() and helper.status(sys_)["keys"] == []


def test_a_key_file_the_service_could_write_is_never_read(tmp_path: Path) -> None:
    sys_ = system(tmp_path)
    for planted in ("var/lib/nox/noxos/ssh_keys.json", "var/lib/nox/authorized_keys"):
        (tmp_path / planted).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / planted).write_text(ED25519 + "\n", "ascii")
    assert helper.stored_keys(sys_) == []
    with pytest.raises(helper.Refused, match="no public key stored"):
        helper.switch_on(sys_, 30, [FP])


@posix
def test_on_needs_exactly_the_confirmed_fingerprints(tmp_path: Path) -> None:
    sys_ = system(tmp_path)
    stored(sys_, ED25519, ECDSA)
    other = parse_key(ED25519_B).fingerprint
    for confirmed in ([FP], [FP, parse_key(ECDSA).fingerprint, other], [other]):
        with pytest.raises(helper.Refused, match="not the ones confirmed"):
            helper.switch_on(sys_, 30, confirmed)
    assert not (tmp_path / "run/nox-os/ssh/authorized_keys").exists()
    answer = helper.switch_on(sys_, 30, [parse_key(ECDSA).fingerprint, FP])
    assert answer["ssh"] == "on"


@posix
def test_on_writes_keys_sudo_and_state_then_starts_sshd_firewall_and_timer(tmp_path: Path) -> None:
    run = FakeRun()
    sys_ = system(tmp_path, run)
    stored(sys_, ED25519)
    request = helper.parse_request(f"ssh on 30 {FP}".encode())
    answer = helper.handle(request, sys_)
    assert answer["ssh"] == "on" and answer["until"] == 1_000_000 + 30 * 60
    assert answer["active"] == [FP]
    assert (tmp_path / "run/nox-os/ssh/authorized_keys").read_text("ascii") == ED25519 + "\n"
    assert (tmp_path / "run/nox-os/sudoers.d/nox-admin").read_text("ascii") == (
        "nox-admin ALL=(ALL:ALL) NOPASSWD: ALL\n"
    )
    state = json.loads((tmp_path / "run/nox-os/ssh.json").read_text("utf-8"))
    assert state["on"] is True and state["keys"] == [FP]
    verbs = [c[:3] for c in run.calls if c[0] == "systemctl" and c[1] != "is-active"]
    assert verbs == [
        ("systemctl", "start", "nox-ssh.service"),
        ("systemctl", "restart", "nox-firewall.service"),
    ]
    timer = next(c for c in run.calls if c[0] == "systemd-run")
    assert "--on-active=30min" in timer and timer[-1] == "--expire"
    assert timer[-5:-1] == ("/usr/bin/python3", "-I", "-S", helper.HELPER)
    # ssh.service is never touched: it stays masked.
    assert not any("ssh.service" in c and "nox-ssh.service" not in c for c in run.calls)


@posix
@pytest.mark.parametrize("failing", ["start nox-ssh", "restart nox-firewall", "systemd-run"])
def test_a_failed_step_switches_everything_off_again(tmp_path: Path, failing: str) -> None:
    run = FakeRun(fail=failing)
    sys_ = system(tmp_path, run)
    stored(sys_, ED25519)
    with pytest.raises(helper.Refused):
        helper.switch_on(sys_, 30, [FP])
    assert not (tmp_path / "run/nox-os/ssh/authorized_keys").exists()
    assert not (tmp_path / "run/nox-os/sudoers.d/nox-admin").exists()
    assert json.loads((tmp_path / "run/nox-os/ssh.json").read_text("utf-8"))["on"] is False
    assert ("systemctl", "stop", "nox-ssh.service") in run.calls


@posix
def test_off_removes_everything_ends_sessions_and_timers(tmp_path: Path) -> None:
    run = FakeRun()
    sys_ = system(tmp_path, run)
    stored(sys_, ED25519)
    helper.switch_on(sys_, 30, [FP])
    answer = helper.switch_off(sys_, "dashboard")
    assert answer["ssh"] == "off" and answer["active"] == []
    assert [k["fingerprint"] for k in answer["keys"]] == [FP]  # stored for next time
    assert not (tmp_path / "run/nox-os/ssh/authorized_keys").exists()
    assert not (tmp_path / "run/nox-os/sudoers.d/nox-admin").exists()
    assert ("systemctl", "stop", "nox-ssh-off-*.timer") in run.calls
    assert ("loginctl", "terminate-user", "nox-admin") in run.calls
    stop = run.calls.index(("systemctl", "stop", "nox-ssh.service"))
    assert ("systemctl", "restart", "nox-firewall.service") in run.calls[stop:]


@posix
def test_the_timer_switches_off_unless_a_later_switch_on_moved_the_end(tmp_path: Path) -> None:
    run = FakeRun()
    sys_ = system(tmp_path, run)
    stored(sys_, ED25519)
    helper.switch_on(sys_, 60, [FP])
    helper.expire(sys_)  # an older timer, firing at 1_000_000: the end is an hour away
    assert (tmp_path / "run/nox-os/ssh/authorized_keys").exists()
    late = helper.System(run=run, clock=lambda: 1_000_000.0 + 3600, root=tmp_path)
    helper.expire(late)
    assert not (tmp_path / "run/nox-os/ssh/authorized_keys").exists()


@posix
def test_status_is_off_unless_sshd_runs(tmp_path: Path) -> None:
    run = FakeRun()
    sys_ = system(tmp_path, run)
    stored(sys_, ED25519)
    helper.switch_on(sys_, 30, [FP])
    assert helper.status(sys_)["ssh"] == "on"
    run.active.clear()  # sshd died
    assert helper.status(sys_) | {} == helper.status(sys_) | {"ssh": "off", "until": None}


# ---- who may ask -------------------------------------------------------------------------

NOX_UID = 999
MAIN = 4242
UNIT_CGROUP = "/system.slice/nox.service"


def _stat(start: int) -> str:
    # pid (comm) state, then fields 4..; the 22nd field overall is the start time.
    after = ["S"] + ["0"] * 18 + [str(start)] + ["0"] * 10
    return f"{MAIN} (nox core) " + " ".join(after) + "\n"


class Proc(helper.System):  # type: ignore[misc,name-defined]
    """A fake /proc and systemd: the core's process, and what an attacker could change."""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__(run=FakeRun(), clock=lambda: 1_000_000.0, root=tmp_path, uid_of=self._uid)
        self.cgroup = f"0::{UNIT_CGROUP}/nox-core\n"
        self.starts = [5000, 5000]
        self.fdinfo_pid = MAIN
        self.living = True

    @staticmethod
    def _uid(name: str) -> int | None:
        return NOX_UID if name == "nox" else None

    def read(self, path: Path) -> str:
        text = str(path).replace("\\", "/")
        if text == f"/proc/{MAIN}/cgroup":
            return self.cgroup
        if text == f"/proc/{MAIN}/stat":
            if not self.starts:
                raise FileNotFoundError(text)
            return _stat(self.starts.pop(0))
        if text.startswith("/proc/self/fdinfo/"):
            return f"pos:\t0\nflags:\t02000002\nPid:\t{self.fdinfo_pid}\n"
        raise FileNotFoundError(text)

    def alive(self, pidfd: int) -> bool:
        return self.living


def proc(tmp_path: Path) -> Proc:
    return Proc(tmp_path)


def test_root_may_ask(tmp_path: Path) -> None:
    assert helper.peer_allowed(helper.Peer(1234, 0), system(tmp_path)) is True


@pytest.mark.parametrize("pidfd", [None, 7])
def test_the_main_process_of_nox_service_may_ask(tmp_path: Path, pidfd: int | None) -> None:
    assert helper.peer_allowed(helper.Peer(MAIN, NOX_UID, pidfd), proc(tmp_path)) is True
    p = proc(tmp_path)
    p.cgroup = f"0::{UNIT_CGROUP}\n"  # without the sandbox's leaf
    assert helper.peer_allowed(helper.Peer(MAIN, NOX_UID, pidfd), p) is True


def test_another_process_or_user_may_not(tmp_path: Path) -> None:
    assert helper.peer_allowed(helper.Peer(MAIN + 1, NOX_UID), proc(tmp_path)) is False
    assert helper.peer_allowed(helper.Peer(MAIN, NOX_UID + 1), proc(tmp_path)) is False


@pytest.mark.parametrize(
    "spoil",
    [
        # A command the core ran: same user, but its own cgroup (the sandbox's) or another unit.
        lambda p: setattr(p, "cgroup", f"0::{UNIT_CGROUP}/run-1\n"),
        lambda p: setattr(p, "cgroup", "0::/user.slice/user-999.slice/session-1.scope\n"),
        # The process that connected is gone and its number belongs to another one now.
        lambda p: setattr(p, "starts", [5000, 6000]),
        lambda p: setattr(p, "starts", []),
        lambda p: setattr(p, "living", False),
        lambda p: setattr(p, "fdinfo_pid", -1),
        lambda p: setattr(p, "fdinfo_pid", MAIN + 1),
    ],
)
def test_a_number_that_is_not_the_same_process_is_refused(tmp_path: Path, spoil: Any) -> None:
    p = proc(tmp_path)
    spoil(p)
    assert helper.peer_allowed(helper.Peer(MAIN, NOX_UID, 7), p) is False


def test_no_main_process_no_answer(tmp_path: Path) -> None:
    p = proc(tmp_path)
    p.run = lambda *a: subprocess.CompletedProcess(a, 0, "MainPID=0\nControlGroup=\n", "")
    assert helper.peer_allowed(helper.Peer(0, NOX_UID), p) is False

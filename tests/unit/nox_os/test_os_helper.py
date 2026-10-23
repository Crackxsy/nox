"""The Nox-OS root helper (os/pi-gen/stage-nox/03-hardening/files/nox_os_helper.py): three
requests and nothing else (keys stored root-owned, switching on only with the
confirmed fingerprints), keys checked exactly as the core checks them, everything it switches on
it switches off again, and only root or nox.service's main process - that very process, not its
number - may ask."""

from __future__ import annotations

import base64
import json
import os
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
PASS = "correct horse battery staple"  # fictitious
PASSLINE = "pass " + base64.b64encode(PASS.encode()).decode()
PASSB = PASSLINE.encode()
OWN_UID = getattr(os, "getuid", lambda: 0)()


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
    return helper.System(
        run=run or FakeRun(),
        clock=lambda: 1_000_000.0,
        root=tmp_path,
        root_uid=OWN_UID,
        uptime=lambda: UPTIME,
    )


#: Seconds since boot in these tests, and this boot's id.
UPTIME = 120.0
BOOT_ID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"


def open_setup(tmp_path: Path, *, until: float = 1800.0, boot_id: str = BOOT_ID) -> Path:
    """Root's setup-mode file as nox_setup.py writes it (this boot, end in seconds since boot)."""
    ident = tmp_path / "proc/sys/kernel/random/boot_id"
    ident.parent.mkdir(parents=True, exist_ok=True)
    ident.write_text(BOOT_ID + "\n", "ascii")
    mode = tmp_path / "run/nox-os/setup-mode"
    mode.parent.mkdir(parents=True, exist_ok=True)
    record = {"v": 1, "boot_id": boot_id, "until": until, "reason": "unclaimed"}
    mode.write_text(json.dumps(record), "ascii")
    return mode


# ---- the requests ----------------------------------------------------------------------------


def test_the_requests() -> None:
    assert helper.parse_request(b"status\n").command == "status"
    assert helper.parse_request(b"ssh off").command == "off"
    assert helper.parse_request(b"keys clear").command == "keys_clear"
    request = helper.parse_request(
        f"keys set\n{PASSLINE}\n{ED25519}\n{ECDSA} admin@example.invalid\n".encode()
    )
    assert (request.command, len(request.keys), request.passphrase) == ("keys_set", 2, PASS)
    fp = FPB.decode()
    request = helper.parse_request(f"ssh on 60 {fp},{fp}\n{PASSLINE}".encode())
    assert (request.command, request.minutes, request.fingerprints) == ("on", 60, (fp, fp))
    assert request.passphrase == PASS and PASS not in repr(request)
    new = "new " + base64.b64encode(b"another long passphrase").decode()
    assert helper.parse_request(f"maint set\n{new}".encode()).command == "maint_set"
    change = helper.parse_request(f"maint change\n{PASSLINE}\n{new}".encode())
    assert (change.passphrase, change.new_passphrase) == (PASS, "another long passphrase")


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
        b"keys set\n" + PASSB + b"\n" + RSA.encode(),
        b"keys set\n" + PASSB + b"\n" + b"\n".join([ED25519.encode()] * 9),
        b"keys set\n" + PASSB + b"\n" + b'command="id" ' + ED25519.encode(),
        "keys set\nssh-ed25519 AAAAé".encode(),
        b"keys clear\n" + ED25519.encode(),
        b"ssh off\n" + ED25519.encode(),
        # The maintenance passphrase: required, readable, and long enough when new.
        b"keys set\n" + ED25519.encode(),
        b"ssh on 60 " + FPB,
        b"ssh on 60 " + FPB + b"\npass not-base64!",
        b"ssh on 60 " + FPB + b"\nnew " + PASSB[5:],
        b"maint set",
        b"maint set\nnew " + base64.b64encode(b"too short"),
        b"maint set\n" + PASSB,
        b"maint change\n" + PASSB,
        b"maint change\nnew " + PASSB[5:] + b"\n" + PASSB,
        b"maint set\nnew " + base64.b64encode(b"long enough but\nwith a newline"),
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
    keys = helper.parse_request(("keys set\n" + PASSLINE + "\n" + "\n".join(lines)).encode()).keys
    helper.set_keys(sys_, keys)


def test_keys_are_stored_root_only_and_answered_with_their_fingerprints(tmp_path: Path) -> None:
    sys_ = system(tmp_path)
    helper.store_passphrase(sys_, PASS)
    request = helper.parse_request(f"keys set\n{PASSLINE}\n{ED25519} laptop\n{ECDSA}\n".encode())
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
    helper.store_passphrase(sys_, PASS)
    request = helper.parse_request(f"ssh on 30 {FP}\n{PASSLINE}".encode())
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


# ---- order, recovery ---------------------------------------------------------------------


@posix
def test_the_sudo_rule_comes_only_after_sshd_runs(tmp_path: Path) -> None:
    sudoers = tmp_path / "run/nox-os/sudoers.d/nox-admin"
    seen: list[bool] = []

    class Watch(FakeRun):
        def __call__(self, *args: str) -> subprocess.CompletedProcess[str]:
            if args[:3] == ("systemctl", "start", "nox-ssh.service"):
                seen.append(sudoers.exists())
            return super().__call__(*args)

    sys_ = system(tmp_path, Watch())
    stored(sys_, ED25519)
    helper.switch_on(sys_, 30, [FP])
    assert seen == [False] and sudoers.exists()
    # sshd that does not come up: no sudo rule at all.
    dead = Watch(fail="is-active")
    sys_ = system(tmp_path / "b", dead)
    stored(sys_, ED25519)
    with pytest.raises(helper.Refused):
        helper.switch_on(sys_, 30, [FP])
    assert not (tmp_path / "b/run/nox-os/sudoers.d/nox-admin").exists()


@posix
def test_a_request_cleans_up_what_sshd_left_on(tmp_path: Path) -> None:
    run = FakeRun()
    sys_ = system(tmp_path, run)
    stored(sys_, ED25519)
    helper.switch_on(sys_, 30, [FP])
    run.active.clear()  # sshd died, or the power went
    answer = helper.handle(helper.parse_request(b"status"), sys_)
    assert answer["ssh"] == "off"
    assert not (tmp_path / "run/nox-os/sudoers.d/nox-admin").exists()
    assert not (tmp_path / "run/nox-os/ssh/authorized_keys").exists()
    state = json.loads((tmp_path / "run/nox-os/ssh.json").read_text("utf-8"))
    assert state == state | {"on": False, "reason": "reconciled"}


# ---- the maintenance passphrase ----------------------------------------------------------


def _req(text: str) -> Any:
    return helper.parse_request(text.encode())


def _new(secret: str) -> str:
    return "new " + base64.b64encode(secret.encode()).decode()


def _pass(secret: str) -> str:
    return "pass " + base64.b64encode(secret.encode()).decode()


def test_without_a_passphrase_keys_and_on_are_refused(tmp_path: Path) -> None:
    sys_ = system(tmp_path)
    for text in (f"keys set\n{PASSLINE}\n{ED25519}", f"ssh on 30 {FP}\n{PASSLINE}"):
        with pytest.raises(helper.Refused) as refused:
            helper.handle(_req(text), sys_)
        assert refused.value.code == "passphrase_missing"
        assert "during setup" in str(refused.value)
    assert helper.status(sys_)["passphrase"] is False


def test_the_passphrase_is_set_only_in_setup_mode(tmp_path: Path) -> None:
    sys_ = system(tmp_path)
    with pytest.raises(helper.Refused) as refused:
        helper.handle(_req(f"maint set\n{_new(PASS)}"), sys_)
    assert refused.value.code == "not_setup"
    mode = tmp_path / "run/nox-os/setup-mode"
    mode.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        os.symlink(tmp_path / "elsewhere", mode)  # a link is not the setup flag
        with pytest.raises(helper.Refused):
            helper.handle(_req(f"maint set\n{_new(PASS)}"), sys_)
        mode.unlink()
    # No window (O3): an empty flag, one whose end is reached, one of an earlier boot.
    for no_window in (
        lambda: mode.write_text("", "ascii"),
        lambda: open_setup(tmp_path, until=UPTIME),
        lambda: open_setup(tmp_path, boot_id="00000000-0000-0000-0000-000000000000"),
    ):
        no_window()
        with pytest.raises(helper.Refused):
            helper.handle(_req(f"maint set\n{_new(PASS)}"), sys_)
    open_setup(tmp_path)
    answer = helper.handle(_req(f"maint set\n{_new(PASS)}"), sys_)
    assert answer["passphrase"] is True and answer["setup_mode"] is True
    record = (tmp_path / "var/lib/nox-os/maintenance").read_text("ascii")
    assert PASS not in record and json.loads(record)["kdf"] == "scrypt"
    if sys.platform != "win32":
        assert (tmp_path / "var/lib/nox-os/maintenance").stat().st_mode & 0o777 == 0o600
    # With it, the keys can be set.
    assert helper.handle(_req(f"keys set\n{PASSLINE}\n{ED25519}"), sys_)["keys"]


def test_the_passphrase_is_changed_only_with_the_old_one(tmp_path: Path) -> None:
    sys_ = system(tmp_path)
    helper.store_passphrase(sys_, PASS)
    with pytest.raises(helper.Refused) as refused:
        helper.handle(
            _req(f"maint change\n{_pass('not the old one')}\n{_new('next passphrase!')}"), sys_
        )
    assert refused.value.code == "passphrase_wrong"
    helper.handle(_req(f"maint change\n{PASSLINE}\n{_new('next passphrase!')}"), sys_)
    helper.verify_passphrase(sys_, "next passphrase!")
    with pytest.raises(helper.Refused):
        helper.verify_passphrase(sys_, PASS)


def test_five_wrong_passphrases_lock_it_and_the_lock_grows(tmp_path: Path) -> None:
    now = [1_000_000.0]
    sys_ = helper.System(run=FakeRun(), clock=lambda: now[0], root=tmp_path, root_uid=OWN_UID)
    helper.store_passphrase(sys_, PASS)
    for _ in range(5):
        with pytest.raises(helper.Refused) as refused:
            helper.verify_passphrase(sys_, "wrong guess number")
        assert refused.value.code == "passphrase_wrong"
    assert helper.locked_until(sys_) == int(now[0] + 15 * 60)
    # Locked: even the right one is refused, and the guess is not even checked.
    with pytest.raises(helper.Refused) as refused:
        helper.verify_passphrase(sys_, PASS)
    assert refused.value.code == "passphrase_locked"
    assert helper.status(sys_)["locked_until"] == int(now[0] + 15 * 60)
    now[0] += 15 * 60 + 1
    for _ in range(5):
        with pytest.raises(helper.Refused):
            helper.verify_passphrase(sys_, "wrong guess number")
    assert helper.locked_until(sys_) == int(now[0] + 30 * 60)  # twice as long
    now[0] += 30 * 60 + 1
    helper.verify_passphrase(sys_, PASS)  # right: the count starts over
    assert not (tmp_path / "var/lib/nox-os/maintenance-lockout").exists()
    with pytest.raises(helper.Refused):
        helper.verify_passphrase(sys_, "wrong guess number")
    assert helper.locked_until(sys_) is None


def test_the_lock_is_at_most_a_day(tmp_path: Path) -> None:
    sys_ = system(tmp_path)
    helper.store_passphrase(sys_, PASS)
    lockout = tmp_path / "var/lib/nox-os/maintenance-lockout"
    lockout.write_text(json.dumps({"failures": 99}), "ascii")
    with pytest.raises(helper.Refused):
        helper.verify_passphrase(sys_, "wrong guess number")
    assert helper.locked_until(sys_) == int(1_000_000.0 + 24 * 3600)


def test_a_passphrase_file_anyone_else_wrote_is_no_passphrase(tmp_path: Path) -> None:
    sys_ = system(tmp_path)
    planted = tmp_path / "var/lib/nox/maintenance"
    planted.parent.mkdir(parents=True)
    planted.write_text('{"hash": "x"}', "ascii")
    with pytest.raises(helper.Refused) as refused:
        helper.verify_passphrase(sys_, PASS)
    assert refused.value.code == "passphrase_missing"


@posix
def test_parallel_wrong_guesses_are_all_counted(tmp_path: Path) -> None:
    """Each helper instance is a process of its own; the lock makes them take turns, so N guesses
    in parallel count N times and the fifth locks - no guess slips past the count."""
    import threading

    sys_ = system(tmp_path)
    helper.store_passphrase(sys_, PASS)
    barrier = threading.Barrier(8)
    codes: list[str] = []

    def guess() -> None:
        own = system(tmp_path)  # its own descriptors, like its own process
        barrier.wait()
        try:
            helper.handle(_req(f"keys set\n{_pass('a parallel wrong guess')}\n{ED25519}"), own)
        except helper.Refused as exc:
            codes.append(exc.code)

    threads = [threading.Thread(target=guess) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    lockout = json.loads((tmp_path / "var/lib/nox-os/maintenance-lockout").read_text("ascii"))
    assert sorted(codes) == ["passphrase_locked"] * 3 + ["passphrase_wrong"] * 5
    assert lockout["failures"] == 5 and lockout["until"] == int(1_000_000.0 + 15 * 60)


def test_maint_set_never_replaces_a_passphrase_without_roots_reset(tmp_path: Path) -> None:
    sys_ = system(tmp_path)
    mode = open_setup(tmp_path)
    helper.handle(_req(f"maint set\n{_new(PASS)}"), sys_)
    # Setup mode again (a second claim), but the passphrase stays without the old one.
    with pytest.raises(helper.Refused) as refused:
        helper.handle(_req(f"maint set\n{_new('somebody elses passphrase')}"), sys_)
    assert refused.value.code == "passphrase_exists"
    helper.verify_passphrase(sys_, PASS)
    # Root's deliberate reset: a flag only root can create, used up by the next set.
    flag = tmp_path / "var/lib/nox-os/reset-maintenance"
    flag.write_text("", "ascii")
    helper.handle(_req(f"maint set\n{_new('a fresh start passphrase')}"), sys_)
    helper.verify_passphrase(sys_, "a fresh start passphrase")
    assert not flag.exists()
    # Without setup mode, not even the flag helps.
    flag.write_text("", "ascii")
    mode.unlink()
    with pytest.raises(helper.Refused) as refused:
        helper.handle(_req(f"maint set\n{_new(PASS)}"), sys_)
    assert refused.value.code == "not_setup"


# ---- the claim (O3) --------------------------------------------------------------------------

CLAIM_CODE = "ABCD-EFGH-JKMN"  # fictitious
LAN_PEER = "192.0.2.30"
NOISY_PEER = "192.0.2.66"
PHONE = "10.42.0.20"


def _code_line(code: str) -> str:
    return "code " + base64.b64encode(code.encode()).decode()


def _claim_req(
    code: str = CLAIM_CODE, device: str = "0a1b2c", new: str | None = None, peer: str = LAN_PEER
) -> Any:
    lines = [f"claim {device} {peer}", _code_line(code)]
    if new is not None:
        lines.append(_new(new))
    return helper.parse_request("\n".join(lines).encode())


def _stage_req(
    code: str = CLAIM_CODE, pin: str | None = None, new: str | None = None, peer: str = PHONE
) -> Any:
    lines = [f"stage {peer}", _code_line(code)]
    if pin is not None:
        lines.append("pin " + base64.b64encode(pin.encode()).decode())
    if new is not None:
        lines.append(_new(new))
    return helper.parse_request("\n".join(lines).encode())


def _claimable(tmp_path: Path) -> Path:
    open_setup(tmp_path)
    record = {"v": 1, "hash": helper.claim_hash("ABCDEFGHJKMN"), "source": "card"}
    state = tmp_path / "var/lib/nox-os/claim.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps(record), "ascii")
    return state


def test_the_claim_and_stage_requests_are_strict() -> None:
    request = _claim_req("abcd efgh jkmn")
    assert (request.command, request.device, request.code) == ("claim", "0a1b2c", "ABCDEFGHJKMN")
    assert request.address == LAN_PEER and "ABCDEFGHJKMN" not in repr(request)
    staged = _stage_req(pin="135790", new=PASS)
    assert (staged.command, staged.address, staged.pin) == ("stage", PHONE, "135790")
    assert "135790" not in repr(staged) and PASS not in repr(staged)
    for bad in (
        b"claim",
        b"claim 0a1b2c\ncode QUJD",  # no address
        b"claim ../x 192.0.2.1\ncode QUJD",
        b"claim 0a1b2c not-an-address\ncode QUJD",
        b"claim 0a1b2c 192.0.2.1\npass QUJD",
        b"stage 10.42.0.20\ncode QUJDREVGR0hKS01O\nkey QUJD",
    ):
        with pytest.raises(helper.Refused):
            helper.parse_request(bad)
    with pytest.raises(helper.Refused) as refused:
        _claim_req("not-a-code!")
    assert refused.value.code == "claim_wrong"
    with pytest.raises(helper.Refused) as refused:
        _stage_req(pin="12")
    assert refused.value.code == "pin_short"


def test_only_root_stages(tmp_path: Path) -> None:
    sys_ = system(tmp_path)
    _claimable(tmp_path)
    with pytest.raises(helper.Refused) as refused:
        helper.handle(_stage_req(pin="135790"), sys_, root=False)
    assert refused.value.code == "not_allowed"
    assert not (tmp_path / "run/nox-os/setup/pending.json").exists()


def test_the_portals_secrets_apply_only_with_the_claim(tmp_path: Path) -> None:
    run = FakeRun()
    sys_ = system(tmp_path, run)
    state = _claimable(tmp_path)
    pending = tmp_path / "run/nox-os/setup/pending.json"
    # A wrong code in the portal keeps nothing.
    with pytest.raises(helper.Refused) as refused:
        helper.handle(_stage_req("ZZZZ-ZZZZ-ZZZZ", pin="135790", new=PASS), sys_)
    assert refused.value.code == "claim_wrong" and not pending.exists()
    # The right one keeps them - and applies nothing yet.
    answer = helper.handle(_stage_req(pin="135790", new=PASS, peer="10.42.0.21"), sys_)
    assert answer["pending_pin"] is True and answer["pending_passphrase"] is True
    assert answer["passphrase"] is False  # not set before the claim
    assert not (tmp_path / "var/lib/nox-os/maintenance").exists()
    if sys.platform != "win32":
        assert pending.stat().st_mode & 0o777 == 0o600
    # A wrong claim applies nothing either.
    open_setup(tmp_path)
    with pytest.raises(helper.Refused):
        helper.handle(_claim_req("ZZZZ-ZZZZ-ZZZZ"), sys_)
    assert not (tmp_path / "var/lib/nox-os/maintenance").exists() and pending.exists()
    # The claim applies both, once, and marks the owner in the same step.
    answer = helper.handle(_claim_req(peer="192.0.2.31"), sys_)
    assert answer["claim"] == "ok" and answer["claimed"] is True
    assert base64.b64decode(answer["pin"]).decode() == "135790"
    helper.verify_passphrase(sys_, PASS)
    marker = json.loads((tmp_path / "var/lib/nox-os-claim/claimed").read_text("ascii"))
    assert marker["owner_device"] == "0a1b2c"
    assert not (tmp_path / "run/nox-os/setup-mode").exists()
    assert not state.exists() and not pending.exists()
    # Hotspot and portal down (waited for) before the firewall opens the node port.
    close = run.calls.index(("systemctl", "start", "nox-setup-close.service"))
    assert close < len(run.calls) - 1 - run.calls[::-1].index(
        ("systemctl", "restart", "nox-firewall.service")
    )
    if sys.platform != "win32":
        assert (tmp_path / "var/lib/nox-os-claim/claimed").stat().st_mode & 0o777 == 0o644
    # Once claimed, never again - not even with the right code in a new window.
    _claimable(tmp_path)
    with pytest.raises(helper.Refused) as refused:
        helper.handle(_claim_req(peer="192.0.2.32"), sys_)
    assert refused.value.code == "claimed"


def test_the_claimant_overrides_what_the_portal_kept(tmp_path: Path) -> None:
    sys_ = system(tmp_path)
    _claimable(tmp_path)
    helper.handle(_stage_req(new="the portal's passphrase"), sys_)
    helper.handle(_claim_req(new=PASS), sys_)
    helper.verify_passphrase(sys_, PASS)


def test_a_claim_never_replaces_a_passphrase(tmp_path: Path) -> None:
    sys_ = system(tmp_path)
    _claimable(tmp_path)
    helper.handle(_req(f"maint set\n{_new(PASS)}"), sys_)
    helper.handle(_claim_req(new="somebody elses passphrase"), sys_)
    helper.verify_passphrase(sys_, PASS)


def test_wrong_codes_slow_their_address_down_not_everyone(tmp_path: Path) -> None:
    uptime = {"now": 100.0}
    sys_ = helper.System(
        run=FakeRun(),
        clock=lambda: 1_000_000.0,
        root=tmp_path,
        root_uid=OWN_UID,
        uptime=lambda: uptime["now"],
    )
    _claimable(tmp_path)
    open_setup(tmp_path, until=10_000)
    codes = []
    for _ in range(4):
        try:
            helper.handle(_claim_req("ZZZZ-ZZZZ-ZZZZ", peer=NOISY_PEER), sys_)
        except helper.Refused as exc:
            codes.append(exc.code)
        uptime["now"] += 1.5
    # 1 s after the first, then 2 s: the third try came too early.
    assert codes == ["claim_wrong", "claim_wrong", "claim_wait", "claim_wrong"]
    record = json.loads((tmp_path / "run/nox-os/claim-failures").read_text("ascii"))
    assert record["kinds"]["lan"]["addresses"][NOISY_PEER]["count"] == 3
    # Another address is not held up.
    assert helper.handle(_claim_req(peer=LAN_PEER), sys_)["claim"] == "ok"


def test_the_backoff_doubles_up_to_five_minutes(tmp_path: Path) -> None:
    uptime = {"now": 100.0}
    sys_ = helper.System(
        run=FakeRun(), root=tmp_path, root_uid=OWN_UID, uptime=lambda: uptime["now"]
    )
    _claimable(tmp_path)
    open_setup(tmp_path, until=100_000)
    for _ in range(12):
        with pytest.raises(helper.Refused):
            helper.handle(_claim_req("ZZZZ-ZZZZ-ZZZZ", peer=NOISY_PEER), sys_)
        uptime["now"] += helper.CLAIM_BACKOFF_MAX_S + 1
    entry = json.loads((tmp_path / "run/nox-os/claim-failures").read_text("ascii"))
    mine = entry["kinds"]["lan"]["addresses"][NOISY_PEER]
    assert (
        mine["until"] - (uptime["now"] - helper.CLAIM_BACKOFF_MAX_S - 1)
        == helper.CLAIM_BACKOFF_MAX_S
    )


def test_staged_secrets_never_exempt_lan_guesses(tmp_path: Path) -> None:
    """A verified-looking pending record changes nothing: every wrong LAN code still counts."""
    uptime = {"now": 100.0}
    sys_ = helper.System(
        run=FakeRun(), root=tmp_path, root_uid=OWN_UID, uptime=lambda: uptime["now"]
    )
    _claimable(tmp_path)
    open_setup(tmp_path, until=100_000)
    helper.handle(_stage_req(pin="135790", new=PASS), sys_)
    pending = tmp_path / "run/nox-os/setup/pending.json"
    record = json.loads(pending.read_text("ascii"))
    assert "verified" not in record
    record["verified"] = True  # whatever got there
    pending.write_text(json.dumps(record), "ascii")
    with pytest.raises(helper.Refused) as refused:
        helper.handle(_claim_req("ZZZZ-ZZZZ-ZZZZ", peer=NOISY_PEER), sys_)
    assert refused.value.code == "claim_wrong"
    with pytest.raises(helper.Refused) as refused:
        helper.handle(_claim_req("ZZZZ-ZZZZ-ZZZZ", peer=NOISY_PEER), sys_)
    assert refused.value.code == "claim_wait"  # backed off, no exemption
    failures = json.loads((tmp_path / "run/nox-os/claim-failures").read_text("ascii"))
    assert failures["kinds"]["lan"]["total"] == 1


def test_the_owner_on_the_hotspot_is_not_held_up_by_a_lan_peer(tmp_path: Path) -> None:
    uptime = {"now": 100.0}
    sys_ = helper.System(
        run=FakeRun(), root=tmp_path, root_uid=OWN_UID, uptime=lambda: uptime["now"]
    )
    _claimable(tmp_path)
    open_setup(tmp_path, until=100_000)
    with pytest.raises(helper.Refused):
        helper.handle(_claim_req("ZZZZ-ZZZZ-ZZZZ", peer=NOISY_PEER), sys_)
    with pytest.raises(helper.Refused) as refused:
        helper.handle(_claim_req(peer=NOISY_PEER), sys_)
    assert refused.value.code == "claim_wait"  # the noisy peer waits, even with the right code
    # The owner, on the hotspot, right now: not held up.
    assert helper.handle(_stage_req(pin="135790"), sys_)["staged"] == "ok"
    # ... nor from another address in the home network.
    assert helper.handle(_claim_req(peer=LAN_PEER), sys_)["claim"] == "ok"


def test_a_spent_budget_slows_every_wrong_code_but_never_locks_the_owner_out(
    tmp_path: Path,
) -> None:
    uptime = {"now": 100.0}
    sys_ = helper.System(
        run=FakeRun(), root=tmp_path, root_uid=OWN_UID, uptime=lambda: uptime["now"]
    )
    _claimable(tmp_path)
    open_setup(tmp_path, until=100_000)
    failures = {
        "boot_id": BOOT_ID,
        "kinds": {"lan": {"total": helper.CLAIM_BUDGET, "addresses": {}}},
    }
    (tmp_path / "run/nox-os/claim-failures").write_text(json.dumps(failures), "ascii")
    with pytest.raises(helper.Refused) as refused:
        helper.handle(_claim_req("ZZZZ-ZZZZ-ZZZZ", peer="192.0.2.41"), sys_)
    assert refused.value.code == "claim_wrong"
    record = json.loads((tmp_path / "run/nox-os/claim-failures").read_text("ascii"))
    first = record["kinds"]["lan"]["addresses"]["192.0.2.41"]
    assert first["until"] == 100.0 + helper.CLAIM_BACKOFF_MAX_S  # the longest wait at once
    assert "hotspot" not in record["kinds"]  # the hotspot's budget is its own
    # A right code from an address that is not waiting still claims.
    assert helper.handle(_claim_req(peer="192.0.2.42"), sys_)["claim"] == "ok"


def test_no_claim_outside_the_window_or_without_a_code(tmp_path: Path) -> None:
    sys_ = system(tmp_path)
    _claimable(tmp_path)
    open_setup(tmp_path, until=UPTIME)  # its end reached
    for request in (_claim_req(), _stage_req(pin="135790")):
        with pytest.raises(helper.Refused) as refused:
            helper.handle(request, sys_)
        assert refused.value.code == "not_setup"
    open_setup(tmp_path)
    (tmp_path / "var/lib/nox-os/claim.json").unlink()
    with pytest.raises(helper.Refused) as refused:
        helper.handle(_claim_req(), sys_)
    assert refused.value.code == "no_claim_code"
    assert helper.status(sys_)["claimed"] is False

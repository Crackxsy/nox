"""The Nox-OS root helper (os/pi-gen/stage-nox/03-hardening/files/nox_os_helper.py): three
requests and nothing else, keys checked exactly as the core checks them, everything it switches on
it switches off again, and only root or nox.service's main process may ask."""

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
            out = "4242\n"
        if self.fail and self.fail in " ".join(args):
            code = 1
        return subprocess.CompletedProcess(args, code, out, "")


def system(tmp_path: Path, run: FakeRun | None = None) -> Any:
    return helper.System(run=run or FakeRun(), clock=lambda: 1_000_000.0, root=tmp_path)


# ---- the requests ----------------------------------------------------------------------------


def test_the_three_requests() -> None:
    assert helper.parse_request(b"status\n").command == "status"
    assert helper.parse_request(b"ssh off").command == "off"
    request = helper.parse_request(
        f"ssh on 60\n{ED25519}\n{ECDSA} admin@example.invalid\n".encode()
    )
    assert (request.command, request.minutes, len(request.keys)) == ("on", 60, 2)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"STATUS",
        b"status extra",
        b"status\nssh-ed25519 AAAA",
        b"ssh on",
        b"ssh on 60",  # no key
        b"ssh on 4\n" + ED25519.encode(),
        b"ssh on 241\n" + ED25519.encode(),
        b"ssh on -5\n" + ED25519.encode(),
        b"ssh on 60 extra\n" + ED25519.encode(),
        b"ssh  on 60\n" + ED25519.encode(),
        b"shell rm -rf /",
        b"ssh on 60\n" + RSA.encode(),
        b"ssh on 60\n" + b"\n".join([ED25519.encode()] * 9),
        b"ssh on 60\n" + b'command="id" ' + ED25519.encode(),
        "ssh on 60\nssh-ed25519 AAAAé".encode(),
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


@posix
def test_on_writes_keys_sudo_and_state_then_starts_sshd_firewall_and_timer(tmp_path: Path) -> None:
    run = FakeRun()
    sys_ = system(tmp_path, run)
    keys = helper.parse_request(f"ssh on 30\n{ED25519}\n".encode()).keys
    answer = helper.switch_on(sys_, 30, keys)
    assert answer["ssh"] == "on" and answer["until"] == 1_000_000 + 30 * 60
    assert (tmp_path / "run/nox-os/ssh/authorized_keys").read_text("ascii") == ED25519 + "\n"
    assert (tmp_path / "run/nox-os/sudoers.d/nox-admin").read_text("ascii") == (
        "nox-admin ALL=(ALL:ALL) NOPASSWD: ALL\n"
    )
    state = json.loads((tmp_path / "run/nox-os/ssh.json").read_text("utf-8"))
    assert state["on"] is True and state["keys"] == [keys[0].fingerprint]
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
    keys = helper.parse_request(f"ssh on 30\n{ED25519}\n".encode()).keys
    with pytest.raises(helper.Refused):
        helper.switch_on(sys_, 30, keys)
    assert not (tmp_path / "run/nox-os/ssh/authorized_keys").exists()
    assert not (tmp_path / "run/nox-os/sudoers.d/nox-admin").exists()
    assert json.loads((tmp_path / "run/nox-os/ssh.json").read_text("utf-8"))["on"] is False
    assert ("systemctl", "stop", "nox-ssh.service") in run.calls


@posix
def test_off_removes_everything_and_ends_the_timers(tmp_path: Path) -> None:
    run = FakeRun()
    sys_ = system(tmp_path, run)
    helper.switch_on(sys_, 30, helper.parse_request(f"ssh on 30\n{ED25519}".encode()).keys)
    answer = helper.switch_off(sys_, "dashboard")
    assert answer["ssh"] == "off" and answer["keys"] == []
    assert not (tmp_path / "run/nox-os/ssh/authorized_keys").exists()
    assert not (tmp_path / "run/nox-os/sudoers.d/nox-admin").exists()
    assert ("systemctl", "stop", "nox-ssh-off-*.timer") in run.calls
    stop = run.calls.index(("systemctl", "stop", "nox-ssh.service"))
    assert ("systemctl", "restart", "nox-firewall.service") in run.calls[stop:]


@posix
def test_the_timer_switches_off_unless_a_later_switch_on_moved_the_end(tmp_path: Path) -> None:
    run = FakeRun()
    sys_ = system(tmp_path, run)
    helper.switch_on(sys_, 60, helper.parse_request(f"ssh on 60\n{ED25519}".encode()).keys)
    helper.expire(sys_)  # an older timer, firing at 1_000_000: the end is an hour away
    assert (tmp_path / "run/nox-os/ssh/authorized_keys").exists()
    late = helper.System(run=run, clock=lambda: 1_000_000.0 + 3600, root=tmp_path)
    helper.expire(late)
    assert not (tmp_path / "run/nox-os/ssh/authorized_keys").exists()


@posix
def test_status_is_off_unless_sshd_runs(tmp_path: Path) -> None:
    run = FakeRun()
    sys_ = system(tmp_path, run)
    helper.switch_on(sys_, 30, helper.parse_request(f"ssh on 30\n{ED25519}".encode()).keys)
    assert helper.status(sys_)["ssh"] == "on"
    run.active.clear()  # sshd died
    assert helper.status(sys_) == helper.status(sys_) | {"ssh": "off", "until": None, "keys": []}


# ---- who may ask -------------------------------------------------------------------------


def test_root_may_ask(tmp_path: Path) -> None:
    assert helper.peer_allowed(1234, 0, system(tmp_path)) is True


@pytest.mark.skipif(sys.platform == "win32", reason="the user database (pwd)")
def test_only_the_main_process_of_nox_service_may_ask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pwd

    class Entry:
        pw_uid = 999

    monkeypatch.setattr(pwd, "getpwnam", lambda name: Entry() if name == "nox" else None)
    sys_ = system(tmp_path)
    assert helper.peer_allowed(4242, 999, sys_) is True  # nox.service's MainPID (FakeRun)
    assert helper.peer_allowed(4243, 999, sys_) is False  # another process of the user nox
    assert helper.peer_allowed(4242, 1000, sys_) is False  # another user

"""nox.supervisor.main with a real fake child: heartbeats, restart rules, safe mode, kill switch,
handshake auth (B-1), graceful stop (B-6)."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import psutil
import pytest

from nox.ipc.protocol import Kind, Source
from nox.shell.supervisor_client import (
    send_supervisor_kill,
    send_supervisor_resume,
    send_supervisor_stop,
)
from nox.supervisor import messages as m
from nox.supervisor.main import (
    Supervisor,
    SupervisorSettings,
    SupervisorState,
    hotkey_blocked_reason,
    hotkey_to_pynput,
    kill_process_tree,
    kill_tree,
    resolve_command,
)

FAKE = Path(__file__).with_name("fake_core.py")
SHELL_SRC = Source(role="shell", id="tray")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def settings(tmp_path: Path, *fake_args: str, **overrides: object) -> SupervisorSettings:
    base: dict[str, object] = {
        "control_port": free_port(),
        "runtime_dir": tmp_path / "runtime",
        "core_command": [sys.executable, str(FAKE), *fake_args],
        "shell_command": None,
        "heartbeat_interval_s": 0.1,
        "missed_for_graceful": 5,
        "missed_for_hard": 10,
        "restart_limit": 1,
        "restart_window_s": 60.0,
        "kill_ack_timeout_s": 0.5,
        "stop_timeout_s": 0.5,
        "shutdown_grace_s": 0.5,
        # The real default is 90 s (a cold boot takes 20-40 s on the product owner's machine);
        # scaled down here like every other timing, and overridden per test where it is the subject.
        "boot_grace_s": 0.4,
        "hotkey_enabled": False,
    }
    base.update(overrides)
    return SupervisorSettings(**base)  # type: ignore[arg-type]


async def wait_until(cond: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


@pytest.fixture
async def sup_factory(tmp_path: Path) -> AsyncIterator[Callable[..., Supervisor]]:
    created: list[Supervisor] = []

    def make(*fake_args: str, **overrides: object) -> Supervisor:
        sup = Supervisor(settings(tmp_path, *fake_args, **overrides))
        created.append(sup)
        return sup

    yield make
    for sup in created:
        await sup.stop()


def _auth_and_request(
    port: int, token: str, name: str, payload: dict[str, object]
) -> dict[str, object]:
    """Raw handshake + one request, for asserting the wire protocol directly (B-1)."""
    auth = m.make(
        m.NAME_AUTH, {"token": token, "role": "shell", "pid": 0}, SHELL_SRC, kind=Kind.REQUEST
    )
    req = m.make(name, payload, SHELL_SRC, kind=Kind.REQUEST)
    with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
        s.sendall(m.encode(auth))
        f = s.makefile("rb")
        auth_reply = json.loads(f.readline())
        if auth_reply.get("name") != m.NAME_AUTH_OK:
            return dict(auth_reply)
        s.sendall(m.encode(req))
        s.shutdown(socket.SHUT_WR)
        data = f.readline()
    return json.loads(data) if data else {}


def test_helpers() -> None:
    assert hotkey_to_pynput("ctrl+alt+shift+k") == "<ctrl>+<alt>+<shift>+k"
    assert hotkey_to_pynput("win+f9") == "<cmd>+<f9>"
    assert resolve_command(["python", "-m", "nox.app"]) == [sys.executable, "-m", "nox.app"]
    assert resolve_command(["custom.exe"]) == ["custom.exe"]
    assert kill_process_tree(0x7FFFFFF0) == []


async def test_healthy_core_keeps_running(
    sup_factory: Callable[..., Supervisor], tmp_path: Path
) -> None:
    sup = sup_factory("--interval", "0.05")
    await sup.start()
    assert m.read_token(tmp_path / "runtime") == sup.token
    await wait_until(lambda: sup.status().core_connected, 5.0)
    await asyncio.sleep(0.6)
    st = sup.status()
    assert st.state is SupervisorState.RUNNING
    assert st.core_pid is not None and psutil.pid_exists(st.core_pid)
    assert st.missed_heartbeats == 0 and st.restarts_in_window == 0
    await sup.stop()
    assert not psutil.pid_exists(st.core_pid)
    assert not m.token_path(tmp_path / "runtime").exists()


def _claim_core(port: int, token: str, payload: dict[str, object]) -> dict[str, object]:
    auth = m.make(
        m.NAME_AUTH,
        {"token": token, "role": "core", **payload},
        Source(role="core", id="impostor"),
        kind=Kind.REQUEST,
    )
    with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
        s.sendall(m.encode(auth))
        return dict(json.loads(s.makefile("rb").readline()))


async def test_a_token_holder_naming_the_core_pid_cannot_become_the_core(
    sup_factory: Callable[..., Supervisor],
) -> None:
    """The shell has the token and `sup.status` names the core's pid: neither is proof."""
    sup = sup_factory("--interval", "0.05")
    await sup.start()
    await wait_until(lambda: sup.status().core_connected, 5.0)
    core_pid = sup.status().core_pid
    assert core_pid is not None

    no_secret = await asyncio.to_thread(_claim_core, sup.port, sup.token, {"pid": core_pid})
    wrong_secret = await asyncio.to_thread(
        _claim_core, sup.port, sup.token, {"pid": core_pid, "core_secret": "x" * 43}
    )

    for reply in (no_secret, wrong_secret):
        assert reply["name"] == m.NAME_ERROR
        assert reply["payload"]["code"] == "auth.denied"  # type: ignore[index]
    await asyncio.sleep(0.3)
    status = sup.status()
    assert status.core_connected and status.missed_heartbeats == 0  # the real core still counts


async def test_the_core_secret_reaches_only_the_core_and_changes_every_spawn(
    sup_factory: Callable[..., Supervisor], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(m.ENV_CORE_SECRET, "inherited-from-a-parent")
    sup = sup_factory()
    shell_env = sup._child_env()
    assert m.ENV_CORE_SECRET not in shell_env  # never inherited, never handed to the shell
    assert shell_env[m.ENV_TOKEN] == sup.token

    await sup.start()
    first = sup._core_secret
    assert first is not None and sup._child_env(core_secret=first)[m.ENV_CORE_SECRET] == first
    await wait_until(lambda: sup.status().core_connected, 5.0)
    old_pid = sup.status().core_pid
    assert old_pid is not None
    await kill_tree(old_pid)  # the next spawn replaces it; nothing may be left running
    sup._spawn_core()
    assert sup._core_secret not in (None, first)

    stale = await asyncio.to_thread(
        _claim_core, sup.port, sup.token, {"pid": 0, "core_secret": first}
    )
    assert stale["name"] == m.NAME_ERROR  # the previous spawn's secret is worthless now


async def test_hung_core_is_restarted_then_safe_mode(
    sup_factory: Callable[..., Supervisor],
) -> None:
    sup = sup_factory("--beats", "3")  # 3 heartbeats, then silent (ignores sup.kill)
    await sup.start()
    first_pid = sup.status().core_pid
    assert first_pid is not None
    # The restart is counted when it begins; the hung process is terminated in a worker thread
    # right after, so "counted" and "gone" are two moments and the test waits for both.
    await wait_until(
        lambda: sup.status().restarts_in_window >= 1 and not psutil.pid_exists(first_pid), 10.0
    )
    await wait_until(lambda: sup.status().state is SupervisorState.SAFE_MODE, 15.0)
    st = sup.status()
    assert st.core_pid is None and "restart limit" in st.safe_mode_reason
    await asyncio.sleep(0.4)
    assert sup.status().state is SupervisorState.SAFE_MODE  # stays there, no respawn loop


async def test_slow_boot_is_not_counted_as_missed_heartbeats(
    sup_factory: Callable[..., Supervisor],
) -> None:
    """A cold boot legitimately takes 20-40 s (imports, vault index); counting missed beats from
    spawn time restarted a core that was still booting (product owner's log, 2026-09-15)."""
    sup = sup_factory("--boot-delay", "1.2", "--interval", "0.05", boot_grace_s=3.0)
    await sup.start()
    await asyncio.sleep(1.0)  # 10 intervals of silence: twice the graceful threshold
    st = sup.status()
    assert st.missed_heartbeats == 0 and st.restarts_in_window == 0
    assert st.state is SupervisorState.RUNNING
    await wait_until(lambda: sup.status().core_connected, 5.0)
    await asyncio.sleep(0.4)
    assert sup.status().restarts_in_window == 0


async def test_core_that_never_heartbeats_is_restarted_after_the_grace(
    sup_factory: Callable[..., Supervisor],
) -> None:
    """The grace is a delay, not an exemption: a core that never reports is still restarted."""
    sup = sup_factory("--boot-delay", "60", boot_grace_s=0.5, restart_limit=5)
    await sup.start()
    await asyncio.sleep(0.3)
    assert sup.status().restarts_in_window == 0  # still inside the grace
    await wait_until(lambda: sup.status().restarts_in_window >= 1, 10.0)


async def test_graceful_restart_request_respawns_the_core(
    sup_factory: Callable[..., Supervisor],
) -> None:
    """`sup.kill mode=restart` + a core that shuts down cleanly = a respawn, never safe mode."""
    sup = sup_factory(
        "--beats", "3", "--restart-ack", "--interval", "0.05", restart_limit=5, boot_grace_s=0.3
    )
    await sup.start()
    await wait_until(lambda: sup.status().core_connected, 5.0)
    first_pid = sup.status().core_pid
    await wait_until(lambda: sup.status().restarts_in_window >= 1, 15.0)
    await wait_until(
        lambda: sup.status().core_connected and sup.status().core_pid not in (None, first_pid),
        15.0,
    )
    assert sup.status().state in (SupervisorState.RUNNING, SupervisorState.RESTARTING)
    assert not sup.status().kill_switch_engaged


async def test_core_exit_is_restarted(sup_factory: Callable[..., Supervisor]) -> None:
    sup = sup_factory("--exit-after", "0.2", restart_limit=5)
    await sup.start()
    await wait_until(lambda: sup.status().restarts_in_window >= 2, 10.0)
    assert sup.status().state in (SupervisorState.RUNNING, SupervisorState.RESTARTING)


async def test_kill_switch_acked_and_resume(sup_factory: Callable[..., Supervisor]) -> None:
    sup = sup_factory("--ack")
    await sup.start()
    await wait_until(lambda: sup.status().core_connected, 5.0)
    pid = sup.status().core_pid
    assert await sup.kill_switch(by="tray", reason="test") is True
    st = sup.status()
    assert st.state is SupervisorState.SAFE_MODE and st.kill_switch_engaged
    assert pid is not None
    await wait_until(lambda: not psutil.pid_exists(pid), 5.0)  # the fake exits after acking
    await asyncio.sleep(0.4)
    assert sup.status().core_pid is None  # safe mode: no automatic restart
    assert await sup.resume(by="tray") is True
    await wait_until(lambda: sup.status().core_connected, 5.0)
    assert sup.status().state is SupervisorState.RUNNING and not sup.status().kill_switch_engaged


async def test_kill_switch_without_ack_terminates(sup_factory: Callable[..., Supervisor]) -> None:
    sup = sup_factory()  # never acks
    await sup.start()
    await wait_until(lambda: sup.status().core_connected, 5.0)
    pid = sup.status().core_pid
    assert pid is not None
    assert await sup.kill_switch(by="hotkey") is False
    assert not psutil.pid_exists(pid)
    assert sup.status().state is SupervisorState.SAFE_MODE


async def test_control_channel_from_shell(sup_factory: Callable[..., Supervisor]) -> None:
    sup = sup_factory("--ack")
    await sup.start()
    await wait_until(lambda: sup.status().core_connected, 5.0)

    reply = await asyncio.to_thread(_auth_and_request, sup.port, sup.token, m.NAME_STATUS, {})
    assert reply["name"] == m.NAME_STATUS and reply["payload"]["state"] == "running"

    reply = await asyncio.to_thread(
        send_supervisor_kill, "127.0.0.1", sup.port, sup.token, reason="panic", origin="tray"
    )
    assert reply["name"] == m.NAME_ACK and reply["payload"]["ok"] is True
    await wait_until(lambda: sup.status().state is SupervisorState.SAFE_MODE, 5.0)
    assert "tray" in sup.status().safe_mode_reason


# ---- B-1: handshake-once auth ------------------------------------------------------------------


async def test_wrong_token_is_denied_and_connection_closed(
    sup_factory: Callable[..., Supervisor],
) -> None:
    sup = sup_factory()
    await sup.start()

    def probe() -> tuple[dict[str, object], bytes]:
        bad = m.make(
            m.NAME_AUTH,
            {"token": "wrong-token-" + "x" * 20, "role": "shell", "pid": 0},
            SHELL_SRC,
            kind=Kind.REQUEST,
        )
        with socket.create_connection(("127.0.0.1", sup.port), timeout=2) as s:
            s.sendall(m.encode(bad))
            f = s.makefile("rb")
            reply = json.loads(f.readline())
            trailing = f.readline()  # the supervisor must have closed the connection
        return reply, trailing

    reply, trailing = await asyncio.to_thread(probe)
    assert reply["name"] == m.NAME_ERROR and reply["payload"]["code"] == "auth.denied"
    assert trailing == b""


async def test_frames_before_auth_are_ignored_not_fatal(
    sup_factory: Callable[..., Supervisor],
) -> None:
    sup = sup_factory()
    await sup.start()

    def probe() -> dict[str, object]:
        heartbeat = m.make(m.NAME_HEARTBEAT, {"pid": 1}, SHELL_SRC)
        status = m.make(m.NAME_STATUS, {}, SHELL_SRC, kind=Kind.REQUEST)  # also pre-auth: dropped
        auth = m.make(
            m.NAME_AUTH,
            {"token": sup.token, "role": "shell", "pid": 1},
            SHELL_SRC,
            kind=Kind.REQUEST,
        )
        with socket.create_connection(("127.0.0.1", sup.port), timeout=2) as s:
            s.sendall(m.encode(heartbeat))
            s.sendall(m.encode(status))
            s.sendall(m.encode(auth))
            f = s.makefile("rb")
            return json.loads(f.readline())

    reply = await asyncio.to_thread(probe)
    assert reply["name"] == m.NAME_AUTH_OK  # the connection survived the pre-auth frames


# ---- B-6: sup.stop graceful shutdown ------------------------------------------------------------


async def test_graceful_stop_exits_within_budget(sup_factory: Callable[..., Supervisor]) -> None:
    sup = sup_factory("--stop-ack")  # simulates NoxCore.stop() completing well inside the budget
    await sup.start()
    await wait_until(lambda: sup.status().core_connected, 5.0)
    pid = sup.status().core_pid
    assert pid is not None
    exited = await sup.graceful_stop(by="shell", reason="user_quit")
    assert exited is True
    await wait_until(lambda: not psutil.pid_exists(pid), 5.0)
    st = sup.status()
    assert st.core_pid is None and not st.core_connected


async def test_hung_core_terminated_after_stop_timeout(
    sup_factory: Callable[..., Supervisor],
) -> None:
    sup = sup_factory()  # no --stop-ack: receives sup.stop and never exits on its own
    await sup.start()
    await wait_until(lambda: sup.status().core_connected, 5.0)
    pid = sup.status().core_pid
    assert pid is not None
    started = time.monotonic()
    exited = await sup.graceful_stop(by="shell", reason="user_quit")
    elapsed = time.monotonic() - started
    assert exited is False  # had to be terminated, did not exit on its own
    assert elapsed < 5.0  # bounded by stop_timeout_s (0.5s here), not left hanging
    assert not psutil.pid_exists(pid)


async def test_shell_quit_sends_sup_stop(sup_factory: Callable[..., Supervisor]) -> None:
    """End-to-end version of `send_supervisor_stop` (the shell's Quit path)."""
    sup = sup_factory("--stop-ack")
    await sup.start()
    await wait_until(lambda: sup.status().core_connected, 5.0)
    pid = sup.status().core_pid
    assert pid is not None

    reply = await asyncio.to_thread(
        send_supervisor_stop, "127.0.0.1", sup.port, sup.token, reason="user_quit"
    )
    assert reply["name"] == m.NAME_ACK and reply["payload"]["ok"] is True
    await wait_until(lambda: not psutil.pid_exists(pid), 5.0)
    assert sup.status().core_pid is None


def test_hotkey_is_not_claimed_where_the_desktop_cannot_deliver_it() -> None:
    """A kill switch that only fires in some windows must not be reported as armed."""
    assert "Wayland" in hotkey_blocked_reason(
        "linux", {"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"}
    )
    assert hotkey_blocked_reason("linux", {"WAYLAND_DISPLAY": "wayland-0"})
    assert hotkey_blocked_reason("linux", {}) == "no graphical session"
    assert hotkey_blocked_reason("linux", {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"}) == ""
    assert hotkey_blocked_reason("win32", {}) == ""
    assert hotkey_blocked_reason("darwin", {}) == ""


async def test_status_reports_the_hotkey_state(sup_factory: Callable[..., Supervisor]) -> None:
    sup = sup_factory("--ack")
    assert sup.status().hotkey == "disabled"  # the test settings turn it off


async def test_a_requested_stop_is_never_answered_with_a_fresh_core(
    sup_factory: Callable[..., Supervisor],
) -> None:
    """After the shell's Quit the watchdog used to see "core gone, state running" and spawn a new
    core, which `stop()` then had to kill in the middle of its boot."""
    sup = sup_factory("--stop-ack")
    await sup.start()
    await wait_until(lambda: sup.status().core_connected, 5.0)

    assert await sup.graceful_stop(reason="user_quit") is True
    await sup._tick()  # the watchdog's next look, forced instead of raced

    status = sup.status()
    assert status.core_pid is None
    assert status.restarts_in_window == 0


# ---- leaving safe mode: the core's rearm and a shell's resume ----------------------------------


async def test_a_resumed_core_rearms_the_watchdog_without_a_restart(
    sup_factory: Callable[..., Supervisor], tmp_path: Path
) -> None:
    """A hotkey kill held the supervisor in safe mode forever: the user resumed in the dashboard,
    the core carried on, and a later crash of that core was never restarted again."""
    rearm_file = tmp_path / "rearm.json"
    sup = sup_factory(
        "--ack-stay", "--resume-after", "0.3", "--rearm-file", str(rearm_file), restart_limit=5
    )
    await sup.start()
    await wait_until(lambda: sup.status().core_connected, 5.0)
    pid = sup.status().core_pid

    assert await sup.kill_switch(by="hotkey", reason="test") is True
    assert sup.status().state is SupervisorState.SAFE_MODE

    await wait_until(lambda: sup.status().state is SupervisorState.RUNNING, 5.0)
    st = sup.status()
    assert st.core_pid == pid  # the resumed core keeps running: re-armed, not restarted
    assert not st.kill_switch_engaged and st.safe_mode_reason == ""
    await wait_until(rearm_file.exists, 5.0)
    assert json.loads(rearm_file.read_text(encoding="utf-8")) == {"ok": True, "reason": ""}

    # The actual bug: after the resume, a crash has to be answered with a fresh core again.
    assert pid is not None
    psutil.Process(pid).kill()
    await wait_until(
        lambda: sup.status().core_pid not in (None, pid) and sup.status().core_connected, 10.0
    )
    assert sup.status().state is SupervisorState.RUNNING


async def test_only_the_core_may_rearm_the_watchdog(sup_factory: Callable[..., Supervisor]) -> None:
    """The shell holds the same control token; it must not be able to talk the supervisor out of
    safe mode while the core - the one place that checks the PIN - stays in it."""
    sup = sup_factory("--ack-stay")
    await sup.start()
    await wait_until(lambda: sup.status().core_connected, 5.0)
    assert await sup.kill_switch(by="hotkey") is True

    reply = await asyncio.to_thread(
        _auth_and_request, sup.port, sup.token, m.NAME_RESUME, {"by": "shell", "rearm": True}
    )

    assert reply["payload"] == {"ok": False, "reason": "not_core"}
    assert sup.status().state is SupervisorState.SAFE_MODE


async def test_a_shell_resume_goes_through_a_running_core(
    sup_factory: Callable[..., Supervisor],
) -> None:
    """With a core connected, restarting it from the supervisor would step around its PIN check
    after a security-path kill; the shell is told to resume through the core instead."""
    sup = sup_factory("--ack-stay")
    await sup.start()
    await wait_until(lambda: sup.status().core_connected, 5.0)
    pid = sup.status().core_pid
    assert await sup.kill_switch(by="tray") is True

    reply = await asyncio.to_thread(send_supervisor_resume, "127.0.0.1", sup.port, sup.token)

    assert reply["name"] == m.NAME_ACK
    assert reply["payload"] == {"ok": False, "reason": "core_running"}
    assert sup.status().state is SupervisorState.SAFE_MODE and sup.status().core_pid == pid


async def test_a_shell_resume_restarts_a_core_that_is_gone(
    sup_factory: Callable[..., Supervisor],
) -> None:
    """After a kill the core never acknowledged, the tree is gone; the tray's `sup.resume` is the
    way back, and it starts a fresh core."""
    sup = sup_factory()  # never acks: the supervisor terminates it
    await sup.start()
    await wait_until(lambda: sup.status().core_connected, 5.0)
    assert await sup.kill_switch(by="hotkey") is False
    assert sup.status().state is SupervisorState.SAFE_MODE

    reply = await asyncio.to_thread(send_supervisor_resume, "127.0.0.1", sup.port, sup.token)

    assert reply["payload"] == {"ok": True, "reason": ""}
    await wait_until(lambda: sup.status().core_connected, 5.0)
    assert sup.status().state is SupervisorState.RUNNING

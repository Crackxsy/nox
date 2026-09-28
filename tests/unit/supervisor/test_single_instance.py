"""A second supervisor exits without touching the running one's token, port or children."""

from __future__ import annotations

import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from nox.core.instance_lock import AlreadyRunningError, InstanceLock
from nox.supervisor import messages as m
from nox.supervisor.main import Supervisor, SupervisorSettings

FAKE = Path(__file__).with_name("fake_core.py")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _settings(runtime_dir: Path, port: int) -> SupervisorSettings:
    return SupervisorSettings(
        control_port=port,
        runtime_dir=runtime_dir,
        core_command=[sys.executable, str(FAKE), "--interval", "0.05"],
        shell_command=None,
        heartbeat_interval_s=0.1,
        stop_timeout_s=0.5,
        shutdown_grace_s=0.5,
        hotkey_enabled=False,
    )


async def test_a_second_supervisor_leaves_the_running_ones_token_and_children(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    port = _free_port()
    first = Supervisor(_settings(runtime, port))
    second = Supervisor(_settings(runtime, port))
    await first.start()
    try:
        core_pid = first.status().core_pid

        with pytest.raises(AlreadyRunningError):
            await second.run()

        assert m.read_token(runtime) == first.token  # the tray can still reach the kill switch
        assert first.status().core_pid == core_pid
        assert second.status().core_pid is None  # it never spawned a core of its own
    finally:
        await first.stop()
    assert not m.token_path(runtime).exists()


async def test_a_busy_control_port_does_not_delete_a_token_it_never_wrote(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    m.token_path(runtime).write_text("someone-elses-token-0123456789", encoding="utf-8")
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        sup = Supervisor(_settings(runtime, int(blocker.getsockname()[1])))

        with pytest.raises(OSError):
            await sup.run()

    assert m.token_path(runtime).read_text(encoding="utf-8") == "someone-elses-token-0123456789"


def test_the_lock_is_exclusive_between_processes_and_freed_when_the_holder_dies(
    tmp_path: Path,
) -> None:
    holder = subprocess.Popen(  # noqa: S603 - fixed argv
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                f"""
                import sys, time
                from pathlib import Path
                from nox.core.instance_lock import InstanceLock
                lock = InstanceLock(Path({str(tmp_path)!r}), "core")
                lock.acquire()
                print("locked", flush=True)
                time.sleep(60)
                """
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        while (line := holder.stdout.readline()) and line.strip() != "locked":
            pass  # log lines before the marker
        assert line.strip() == "locked"
        with pytest.raises(AlreadyRunningError, match="already running"):
            InstanceLock(tmp_path, "core").acquire()
    finally:
        holder.kill()
        holder.wait()

    lock = InstanceLock(tmp_path, "core")
    lock.acquire(wait_s=5.0)  # the kernel released the dead holder's lock
    assert lock.held
    lock.release()


def test_a_waiting_acquire_gives_up_after_its_deadline(tmp_path: Path) -> None:
    held = InstanceLock(tmp_path, "core")
    held.acquire()
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    try:
        with pytest.raises(AlreadyRunningError):
            InstanceLock(tmp_path, "core").acquire(wait_s=1.0, clock=lambda: now[0], sleep=sleep)
        assert now[0] >= 1.0
    finally:
        held.release()

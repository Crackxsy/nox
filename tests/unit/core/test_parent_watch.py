"""`bind_to_parent`: an orphaned Nox child process ends itself instead of running unsupervised."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time

import psutil
import pytest

from nox.core.parent_watch import PARENT_PID_ENV, bind_to_parent, parent_alive, parent_env

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="Windows uses a job object")


def _record() -> tuple[threading.Event, object]:
    fired = threading.Event()
    return fired, fired.set


def test_parent_env_names_this_process() -> None:
    assert parent_env() == {PARENT_PID_ENV: str(os.getpid())}


def test_no_watcher_without_a_nox_parent() -> None:
    fired, callback = _record()
    assert bind_to_parent(environ={}, platform="linux", on_orphaned=callback) is None
    assert (
        bind_to_parent(environ={PARENT_PID_ENV: "x"}, platform="linux", on_orphaned=callback)
        is None
    )
    assert not fired.is_set()


def test_no_watcher_on_windows_where_the_job_object_does_this() -> None:
    fired, callback = _record()
    env = {PARENT_PID_ENV: str(os.getpid())}
    assert bind_to_parent(environ=env, platform="win32", on_orphaned=callback) is None
    assert not fired.is_set()


def test_a_live_parent_is_left_alone() -> None:
    fired, callback = _record()
    env = {PARENT_PID_ENV: str(os.getpid())}
    watcher = bind_to_parent(
        environ=env, platform="linux", on_orphaned=callback, poll_interval_s=0.02
    )
    assert watcher is not None and watcher.daemon
    assert not fired.wait(0.2)


def test_a_parent_that_is_already_gone_is_reported_at_once() -> None:
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    fired, callback = _record()
    env = {PARENT_PID_ENV: str(finished.pid)}
    assert bind_to_parent(environ=env, platform="linux", on_orphaned=callback) is None
    assert fired.is_set()


def test_a_parent_that_dies_later_is_noticed() -> None:
    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        fired, callback = _record()
        env = {PARENT_PID_ENV: str(parent.pid)}
        bind_to_parent(environ=env, platform="linux", on_orphaned=callback, poll_interval_s=0.02)
        assert not fired.wait(0.1)
        parent.kill()
        parent.wait()
        assert fired.wait(5.0)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()


def test_a_recycled_pid_is_not_mistaken_for_the_parent() -> None:
    created = psutil.Process(os.getpid()).create_time()
    assert parent_alive(os.getpid(), created)
    assert not parent_alive(os.getpid(), created - 100.0)


@posix_only
def test_an_orphaned_child_process_really_exits() -> None:
    """End to end: parent spawns a child that binds to it, the parent is killed, the child goes."""
    child_code = textwrap.dedent(
        """
        import time
        from nox.core import parent_watch
        parent_watch.bind_to_parent(poll_interval_s=0.05)
        time.sleep(30)
        """
    )
    parent_code = textwrap.dedent(
        f"""
        import os, subprocess, sys, time
        env = dict(os.environ, {PARENT_PID_ENV}=str(os.getpid()))
        child = subprocess.Popen([sys.executable, "-c", {child_code!r}], env=env)
        print(child.pid, flush=True)
        time.sleep(30)
        """
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code], stdout=subprocess.PIPE, text=True
    )
    try:
        assert parent.stdout is not None
        child_pid = int(parent.stdout.readline())
        time.sleep(0.5)  # the child has bound to its parent
        assert psutil.pid_exists(child_pid)
        parent.kill()
        parent.wait()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                if psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE:
                    break
            except psutil.NoSuchProcess:
                break
            time.sleep(0.05)
        else:
            pytest.fail("the orphaned child kept running")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()

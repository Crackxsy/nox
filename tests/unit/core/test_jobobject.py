"""nox.core.jobobject: closing the job kills assigned children (Windows); no-op elsewhere."""

from __future__ import annotations

import subprocess
import sys
import time

import psutil
import pytest

from nox.core.jobobject import JobObject


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
def test_close_kills_assigned_process() -> None:
    job = JobObject("nox-test-job")
    assert job.available
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert job.assign(child.pid) is True
        assert child.pid in job.assigned
        assert job.close() is True
        deadline = time.monotonic() + 5
        while child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert child.poll() is not None
        assert not psutil.pid_exists(child.pid) or psutil.Process(child.pid).status() == "zombie"
    finally:
        if child.poll() is None:
            child.kill()
    assert job.available is False
    assert job.assign(child.pid) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
def test_assign_unknown_pid_fails_gracefully() -> None:
    with JobObject() as job:
        assert job.assign(0x7FFFFFFF) is False


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows no-op")
def test_noop_off_windows() -> None:
    job = JobObject()
    assert job.available is False
    assert job.assign(1) is False
    assert job.close() is False

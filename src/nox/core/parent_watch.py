"""A child process ends itself when the Nox process that spawned it is gone.

On Windows the spawning process puts its children into a job object with kill-on-close, so the
kernel ends the whole tree when the parent dies. POSIX has no equivalent that covers macOS and
Linux alike, and without one a crashed supervisor or core leaves its core, voice worker and plugin
workers running - still holding a microphone, a screen capture or a chat connection nobody
supervises - and every restart adds another set.

Every spawner therefore passes its own pid as `NOX_PARENT_PID`. `bind_to_parent()`, called first
thing in each child's entry point, starts a daemon thread that checks that exact process (pid *and*
start time, so a recycled pid is not mistaken for the parent) once per `POLL_INTERVAL_S`. When the
parent is gone the child sends itself SIGTERM - the core shuts down cleanly on it - and exits hard
if it is still alive `EXIT_GRACE_S` later.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from collections.abc import Callable, Mapping

import psutil

from nox.core.logging import get_logger

log = get_logger(__name__)

#: Set by every Nox process that spawns another one, read by the child.
PARENT_PID_ENV = "NOX_PARENT_PID"

#: How often the child checks its parent. Bounds how long an orphan survives.
POLL_INTERVAL_S = 1.0

#: How long a clean shutdown after SIGTERM may take before the child exits hard.
EXIT_GRACE_S = 10.0


def parent_env() -> dict[str, str]:
    """The variable a spawner adds to its child's environment."""
    return {PARENT_PID_ENV: str(os.getpid())}


def _identity(pid: int) -> float | None:
    """The process's start time, or None when no live (non-zombie) process has this pid."""
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return None
        return proc.create_time()
    except (psutil.Error, OSError):
        return None


def parent_alive(pid: int, created: float) -> bool:
    """True while the process that was `pid` at `created` still runs."""
    current = _identity(pid)
    return current is not None and abs(current - created) < 0.01


def _terminate_self() -> None:
    log.warning("process.parent_gone", note="ending this orphaned process")
    os.kill(os.getpid(), signal.SIGTERM)
    threading.Event().wait(EXIT_GRACE_S)
    log.error("process.exit_after_grace", grace_s=EXIT_GRACE_S)
    os._exit(1)


def bind_to_parent(
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    on_orphaned: Callable[[], None] = _terminate_self,
    poll_interval_s: float = POLL_INTERVAL_S,
) -> threading.Thread | None:
    """Watch `NOX_PARENT_PID` and call `on_orphaned` once it is gone. Returns the watcher, if any.

    No watcher on Windows (the job object already does this, in the kernel) or when the process
    was not started by Nox (no or an unparsable `NOX_PARENT_PID`). A parent that is already gone
    at this point is reported immediately.
    """
    environ = environ if environ is not None else os.environ
    platform = platform if platform is not None else sys.platform
    if platform == "win32":
        return None
    raw = environ.get(PARENT_PID_ENV, "").strip()
    if not raw.isdigit():
        return None
    pid = int(raw)
    created = _identity(pid)
    if created is None:
        on_orphaned()
        return None

    def watch() -> None:
        stop = threading.Event()
        while not stop.wait(poll_interval_s):
            if not parent_alive(pid, created):
                on_orphaned()
                return

    thread = threading.Thread(target=watch, name="nox-parent-watch", daemon=True)
    thread.start()
    return thread


__all__ = ["PARENT_PID_ENV", "bind_to_parent", "parent_alive", "parent_env"]

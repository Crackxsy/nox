"""One supervisor and one core per runtime directory, enforced by the operating system.

A second supervisor used to write its own control token over the running one's and delete it again
when it failed to bind the control port, which broke the tray's kill switch; a second core
overwrote `session.token` and `ipc.json` and appended to the same audit chain. Each of them now
takes an exclusive lock on `<runtime_dir>/<name>.lock` before it touches anything, and a second
instance fails with `AlreadyRunningError` instead.

The lock is an OS lock on an open file (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows), not
the file's existence: the kernel releases it when the process ends, however it ends, so a crashed
instance never leaves a stale lock behind. The file holds the owner's pid for a person looking at
it; nothing reads it back.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import IO

from nox.core.logging import get_logger

log = get_logger(__name__)

__all__ = ["LOCK_RETRY_INTERVAL_S", "AlreadyRunningError", "InstanceLock"]

#: How often a waiting acquire tries again.
LOCK_RETRY_INTERVAL_S = 0.1


class AlreadyRunningError(RuntimeError):
    """Another process holds the lock: this instance must exit without touching anything."""


class InstanceLock:
    """An exclusive, process-lifetime lock named `<runtime_dir>/<name>.lock`."""

    def __init__(self, runtime_dir: Path, name: str) -> None:
        self.path = runtime_dir / f"{name}.lock"
        self._name = name
        self._handle: IO[str] | None = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(
        self,
        *,
        wait_s: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Take the lock, retrying for up to `wait_s` seconds. Raises `AlreadyRunningError`.

        Waiting covers a restart that overlaps the previous instance's last second of shutdown.
        """
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = clock() + wait_s
        while True:
            handle = open(self.path, "a+", encoding="utf-8")  # noqa: SIM115 - held until release()
            if _try_lock(handle):
                break
            handle.close()
            if clock() >= deadline:
                raise AlreadyRunningError(
                    f"another Nox {self._name} is already running (lock: {self.path})"
                )
            sleep(LOCK_RETRY_INTERVAL_S)
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self._handle = handle
        log.info("instance.lock_acquired", name=self._name)

    def release(self) -> None:
        """Give the lock back. The file stays; its existence means nothing without the lock."""
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            _unlock(handle)
        finally:
            handle.close()

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


if sys.platform == "win32":
    import msvcrt

    def _try_lock(handle: IO[str]) -> bool:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(handle: IO[str]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(handle: IO[str]) -> bool:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(handle: IO[str]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

"""Windows job object wrapper (Process Model: "job objects group each worker with its children").

`JobObject` creates a job with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`; every process assigned to it
(and its descendants) is terminated when the job handle is closed - including when the owning
process dies. Off Windows (or when the Win32 call fails) every method is a logged no-op returning
False, so callers degrade to psutil tree kills.

The other half of the guarantee lives in the child: `die_with_parent` is what a worker calls on
Linux and macOS, where no job object exists, so a worker or plugin never outlives a core that
crashed or was killed - on a headless Raspberry Pi nobody would notice the orphans otherwise.
"""

from __future__ import annotations

import ctypes
import os
import signal
import sys
import threading
import time
from ctypes import wintypes

from nox.core.logging import get_logger

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class JobObject:
    """Create with `JobObject(name)`; `assign(pid)` processes; `close()` kills everything
    assigned."""

    def __init__(self, name: str | None = None) -> None:
        self._log = get_logger(__name__)
        self._handle: int | None = None
        self._assigned: set[int] = set()
        if sys.platform != "win32":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32 = kernel32
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        handle = kernel32.CreateJobObjectW(None, name)
        if not handle:
            self._log.warning("jobobject.create_failed", error=ctypes.get_last_error())
            return
        info = _ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
        )
        if not ok:
            self._log.warning("jobobject.limit_failed", error=ctypes.get_last_error())
            kernel32.CloseHandle(handle)
            return
        self._handle = int(handle)

    @property
    def available(self) -> bool:
        return self._handle is not None

    @property
    def assigned(self) -> frozenset[int]:
        return frozenset(self._assigned)

    def assign(self, pid: int) -> bool:
        """Put `pid` into the job. False when unavailable or the process could not be assigned."""
        if self._handle is None:
            return False
        proc = self._k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not proc:
            self._log.warning("jobobject.open_failed", pid=pid, error=ctypes.get_last_error())
            return False
        try:
            if not self._k32.AssignProcessToJobObject(self._handle, proc):
                self._log.warning("jobobject.assign_failed", pid=pid, error=ctypes.get_last_error())
                return False
        finally:
            self._k32.CloseHandle(proc)
        self._assigned.add(pid)
        return True

    def close(self) -> bool:
        """Close the job: every assigned process (and its children) is terminated by Windows."""
        if self._handle is None:
            return False
        self._k32.CloseHandle(self._handle)
        self._handle = None
        self._assigned.clear()
        return True

    def __enter__(self) -> JobObject:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


#: `prctl` option number from <linux/prctl.h>.
_PR_SET_PDEATHSIG = 1


def die_with_parent(poll_s: float = 2.0) -> bool:
    """End this process when the process that spawned it ends (POSIX; a no-op on Windows).

    Two mechanisms, because each covers a gap of the other:

    * Linux `PR_SET_PDEATHSIG` delivers SIGTERM the moment the parent dies. The kernel ties it to
      the parent *thread* that forked us; the core spawns workers from its event-loop thread,
      which lives as long as the core. A spawn moved to a short-lived executor thread would
      trigger it early - keep spawning on the loop thread.
    * Everywhere (and the only mechanism on macOS), a daemon thread notices re-parenting, the
      portable sign that the parent is gone.

    Both end the process with SIGTERM, so its own shutdown handling still runs - the voice worker
    releases the audio device instead of being cut off. Returns True when protection is on.
    """
    if sys.platform == "win32":
        return False
    parent = os.getppid()
    if sys.platform.startswith("linux"):
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(_PR_SET_PDEATHSIG, int(signal.SIGTERM), 0, 0, 0) != 0:
                get_logger(__name__).warning("jobobject.pdeathsig_failed", errno=ctypes.get_errno())
        except (OSError, AttributeError) as exc:
            get_logger(__name__).warning("jobobject.pdeathsig_failed", error=str(exc))
    if os.getppid() != parent:
        _terminate_self()  # the parent died before protection was in place

    def watch() -> None:
        while True:
            time.sleep(poll_s)
            if os.getppid() != parent:
                _terminate_self()
                return

    threading.Thread(target=watch, name="nox-parent-watch", daemon=True).start()
    return True


def _terminate_self(grace_s: float = 5.0) -> None:
    """SIGTERM to ourselves; if the process is still there after `grace_s`, exit hard."""
    os.kill(os.getpid(), signal.SIGTERM)
    time.sleep(grace_s)
    os._exit(0)

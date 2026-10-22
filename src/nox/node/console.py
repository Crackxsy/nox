"""Whether the machine the core runs on is being used remotely right now - an approval given
through a remote desktop could come from the very PC Nox is controlling.

True, False, or None when it cannot be told (the caller then treats it as remote: fail
closed). Third-party remote tools that draw into the console session itself (Parsec, AnyDesk
and the like) cannot be told apart from a person at the machine; the security PIN is the guard
against those (SECURITY.md, residual risk).
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from typing import Any

__all__ = ["console_is_remote"]

SM_REMOTESESSION = 0x1000


def _windows() -> bool | None:
    if not hasattr(ctypes, "windll"):
        return None
    windll: Any = ctypes.windll  # type: ignore[attr-defined,unused-ignore]
    try:
        if windll.user32.GetSystemMetrics(SM_REMOTESESSION):
            return True
        session = ctypes.c_ulong(0)
        if not windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
            return None
        console = int(windll.kernel32.WTSGetActiveConsoleSessionId())
    except (OSError, AttributeError):
        return None
    if console == 0xFFFFFFFF:
        return None  # no session attached to the console right now
    return int(session.value) != console


def _loginctl_remote(session_id: str, run: Callable[[list[str]], str]) -> bool | None:
    try:
        out = run(["loginctl", "show-session", session_id, "-p", "Remote", "--value"]).strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if out in ("yes", "no"):
        return out == "yes"
    return None


def _run(argv: list[str]) -> str:
    path = shutil.which(argv[0])
    if path is None:
        raise OSError(f"{argv[0]} not found")
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [path, *argv[1:]], capture_output=True, text=True, timeout=3, check=False
    )
    return done.stdout


def _posix(env: Mapping[str, str], run: Callable[[list[str]], str]) -> bool | None:
    if any(env.get(name) for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")):
        return True
    session = env.get("XDG_SESSION_ID", "")
    if not session:
        return None
    return _loginctl_remote(session, run)


def console_is_remote(
    *,
    platform: str = sys.platform,
    env: Mapping[str, str] | None = None,
    run: Callable[[list[str]], str] = _run,
) -> bool | None:
    """Whether this process runs in a remote session; None when that cannot be told."""
    if platform == "win32":
        return _windows()
    return _posix(os.environ if env is None else env, run)

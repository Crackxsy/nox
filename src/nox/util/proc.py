"""Subprocess spawning details that must be identical everywhere: the Windows creation flags that
keep a console window from flashing up, and locating an external binary on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
import sys


def creation_flags() -> int:
    """Flags for `subprocess`/`asyncio.create_subprocess_exec` so no console window appears.

    Windows opens a console for every child of a GUI-less process; `CREATE_NO_WINDOW` suppresses
    it. On other platforms there is nothing to suppress, hence 0.
    """
    if sys.platform != "win32":
        return 0
    return subprocess.CREATE_NO_WINDOW


def find_binary(name: str) -> str | None:
    """Full path of an external tool on PATH, or None when it is not installed.

    Deliberately probed at call time rather than cached: a driver or tool installed while Nox is
    running becomes usable without a restart, and one uninstalled stops being reported as present.
    """
    return shutil.which(name)

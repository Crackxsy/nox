"""The very first thing the core does on Nox-OS: make itself non-dumpable.

The root helper trusts the main process of nox.service, not the user `nox`: commands Nox runs for
someone run as that user too. While the core is dumpable, such a process could trace it or read
and write its memory (`/proc/<pid>/mem`) and so speak to the helper as the core - Raspberry Pi OS's
kernel has no Yama to stop it. `PR_SET_DUMPABLE` 0 puts the core out of their reach and changes
nothing for the core itself; a child that starts another program is dumpable again.

It has to happen before the core starts anything: `nox core` calls `protect_on_nox_os()` as its
first statement, before the event loop, the extensions or any subprocess
(`tests/unit/noxos/test_protect.py` holds it to that). Standard library only, so importing it is
cheap and starts nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = ["OS_RELEASE", "protect_core", "protect_on_nox_os", "protected"]

OS_RELEASE = Path("/etc/nox-os-release")
_PR_SET_DUMPABLE = 4
#: None until `protect_on_nox_os` ran; then whether the core is non-dumpable.
_STATE: dict[str, bool | None] = {"protected": None}


def protect_core() -> bool:
    """`prctl(PR_SET_DUMPABLE, 0)`; True when it took. Linux only."""
    if sys.platform != "linux":
        return False
    import ctypes  # noqa: PLC0415 - Linux only

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return int(libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0)) == 0
    except (OSError, AttributeError):
        return False


def protect_on_nox_os(release: Path = OS_RELEASE) -> bool | None:
    """On Nox-OS: protect the core and remember the outcome (`protected()`). Elsewhere: nothing,
    None."""
    if not release.is_file():
        return None
    _STATE["protected"] = protect_core()
    return _STATE["protected"]


def protected() -> bool | None:
    """What `protect_on_nox_os` found: None if it never ran or this is no Nox-OS."""
    return _STATE["protected"]

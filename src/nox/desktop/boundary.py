"""What Nox may never close, kill or pull to the front - regardless of profile or confirmation.

A risk level is a question the user can answer. This file is the set of things where the answer must
not be asked, because a yes would be a mistake nobody meant to make. It is the same shape as
`nox.home.boundary`, which keeps door locks out of the smart-home tools entirely rather than marking
them high risk.

Three groups, each for its own reason:

* **Windows itself.** Terminating `csrss.exe` or `winlogon.exe` does not close a program, it takes
  the session down with it. There is no context in which a language model should be able to try.
* **Nox.** Its own core, shell and workers. A tool that can kill the process holding the audit log
  and the kill switch is a kill switch with no record.
* **The game.** Rocket League is observation-only, and that is not only about input: closing the
  game mid-match, or yanking focus away from it, is the same broken promise by another route.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

from nox.core.logging import get_logger

log = get_logger(__name__)

__all__ = ["CRITICAL_PROCESSES", "BoundaryError", "check_process", "is_critical"]


class BoundaryError(Exception):
    """A target that is out of bounds. Raised before anything is attempted."""


#: Lower-case executable names. Terminating any of these ends the session or the desktop rather
#: than a program, so none of them is a decision to put in a dialog.
CRITICAL_PROCESSES: frozenset[str] = frozenset(
    {
        "system",
        "registry",
        "memory compression",
        "smss.exe",
        "csrss.exe",
        "wininit.exe",
        "winlogon.exe",
        "services.exe",
        "lsass.exe",
        "svchost.exe",
        "fontdrvhost.exe",
        "dwm.exe",
        "logonui.exe",
        "audiodg.exe",
        # Not fatal, and still not something to let happen by accident: the taskbar, the desktop
        # and every open folder window disappear at once.
        "explorer.exe",
    }
)

#: Nox's own executables. `python.exe` is deliberately absent - refusing every Python process would
#: refuse half the machine - so the check uses the process tree instead, and this list only catches
#: a packaged build.
NOX_PROCESSES: frozenset[str] = frozenset({"nox.exe", "nox-shell.exe", "nox-worker.exe"})


def is_critical(process_name: str) -> bool:
    return process_name.strip().lower() in CRITICAL_PROCESSES


def _own_tree() -> set[int]:
    """This process, its parent and its children, as far as they can be discovered."""
    own = {os.getpid()}
    try:
        own.add(os.getppid())
    except (AttributeError, OSError):  # pragma: no cover - not on Windows
        pass
    return own


def check_process(
    pid: int,
    process_name: str,
    *,
    own_pids: Iterable[int] = (),
    game_pids: Iterable[int] = (),
) -> None:
    """Raise `BoundaryError` when this process is one of the three untouchable groups.

    `own_pids` and `game_pids` are passed in rather than discovered here, because who counts as
    "Nox" depends on how it was started and who counts as "the game" is the sensor's answer, not
    this module's guess.
    """
    name = process_name.strip().lower()
    if is_critical(name):
        raise BoundaryError(
            f"{process_name} is part of Windows itself; ending it would end the session"
        )
    if name in NOX_PROCESSES or pid in set(own_pids) | _own_tree():
        raise BoundaryError("that is Nox itself; use the kill switch if you want it to stop")
    if pid in set(game_pids):
        raise BoundaryError(
            "the game is observed only - Nox does not close it or take focus from it"
        )

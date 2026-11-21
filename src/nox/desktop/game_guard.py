"""Whether a game keeps Nox's hands off this machine right now - read fresh, fail closed.

One question, asked by every core-side input guard (`nox.desktop.install`): is a game running,
or in front, or does something own the screen. A game is recognised four ways, so that none of
them has to be perfect:

* its process is on the watched-game list (`sensors.game.process_names`);
* anti-cheat software runs - a process, a service or a kernel driver (`nox.desktop.anticheat`);
* a watched game or an anti-cheat process is in front, or the foreground cannot be read;
* the shell says something owns the screen: a full-screen Direct3D app, "busy" or presentation
  mode. A game that is on no list and has no anti-cheat is caught here.

Any check that cannot answer says "locked": a lock that fails open is no lock. The answer is a
`GameLockState` with a stable `code` and a plain-words `cause`, so the user can be told why Nox
holds still instead of just being refused. A node answers the same question in
`nox.node.device.world.GameLock`; `tests/unit/desktop/test_game_guard.py` holds the two to the
same verdict for every combination of inputs.

This module only observes: process names, service state and the shell's notification state.
Nothing here sends input or reads another process's memory.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from nox.desktop.anticheat import (
    LOCKING_NOTIFICATION_STATES,
    fullscreen_state,
    is_anti_cheat,
    running_services,
)
from nox.desktop.computer_use import foreground_pid
from nox.desktop.process_probe import process_name_of, running_names_on_machine

__all__ = ["CODE_IN_FRONT", "CODE_RUNNING", "GameGuard", "GameLockState"]

#: A watched game or anti-cheat software runs, or that cannot be told.
CODE_RUNNING = "game_running"
#: A game is in front or owns the screen, or that cannot be told.
CODE_IN_FRONT = "game_in_foreground"


@dataclass(frozen=True, slots=True)
class GameLockState:
    """Why input is locked: `code` is stable (for a status or a dashboard to switch on), `cause`
    is a sentence fragment for a person."""

    code: str
    cause: str


def _foreground_name() -> str:
    return process_name_of(foreground_pid())


@dataclass
class GameGuard:
    """The game lock of a core. Every callable is read on every question; none is cached."""

    watched: Callable[[], Iterable[str]]
    running_names: Callable[[], set[str]] = running_names_on_machine
    foreground_name: Callable[[], str] = _foreground_name
    services: Callable[[], set[str]] = running_services
    screen_state: Callable[[], int] = fullscreen_state

    def _names(self) -> set[str]:
        return {name.strip().lower() for name in self.watched() if name.strip()}

    def _running_cause(self) -> str | None:
        try:
            names = self.running_names()
            if names & self._names():
                return "a game on the watched list is running"
            if any(is_anti_cheat(name) for name in names):
                return "anti-cheat software is running"
            if self.services():
                return "an anti-cheat service or driver is running"
        except Exception:  # noqa: BLE001 - cannot tell: locked
            return "the list of running programs could not be read"
        return None

    def _in_front_cause(self) -> str | None:
        try:
            name = self.foreground_name().strip().lower()
            if not name:
                return "the program in front could not be identified"
            if name in self._names() or is_anti_cheat(name):
                return "a game is in front"
            if self.screen_state() in LOCKING_NOTIFICATION_STATES:
                return "a full-screen program or presentation owns the screen"
        except Exception:  # noqa: BLE001 - cannot tell: locked
            return "the state of the screen could not be read"
        return None

    def running(self) -> bool:
        """A watched game or anti-cheat software runs (True when that cannot be told)."""
        return self._running_cause() is not None

    def in_front(self) -> bool:
        """A game is in front or owns the screen (True when that cannot be told)."""
        return self._in_front_cause() is not None

    def lock(self) -> GameLockState | None:
        """Why no input may happen now, or None. Running outranks in front, as on a node."""
        cause = self._running_cause()
        if cause is not None:
            return GameLockState(CODE_RUNNING, cause)
        cause = self._in_front_cause()
        if cause is not None:
            return GameLockState(CODE_IN_FRONT, cause)
        return None

    def reason(self) -> str | None:
        """The stable code of `lock()` (`game_running`, `game_in_foreground`) or None."""
        state = self.lock()
        return None if state is None else state.code

    def cause(self) -> str:
        """The plain-words cause of `lock()`, or an empty string while input is free."""
        state = self.lock()
        return "" if state is None else state.cause

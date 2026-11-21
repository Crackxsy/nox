"""The game lock: while a watched game runs in front, the device does not capture or drive the
desktop."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass


def default_games() -> tuple[str, ...]:
    from nox.core.config.assistant import SensorGameConfig  # noqa: PLC0415

    return tuple(SensorGameConfig().process_names)


def _foreground_name() -> str:
    from nox.desktop.computer_use import foreground_pid  # noqa: PLC0415
    from nox.desktop.process_probe import process_name_of  # noqa: PLC0415

    return process_name_of(foreground_pid())


def _running_services() -> set[str]:
    from nox.desktop.anticheat import running_services  # noqa: PLC0415

    return running_services()


def _screen_state() -> int:
    from nox.desktop.anticheat import fullscreen_state  # noqa: PLC0415

    return fullscreen_state()


@dataclass
class GameLock:
    """Whether a game keeps Nox's hands off this device right now - fail closed.

    Locked while any of these holds, each read fresh on every call:

    * a watched game runs: the core's default list, this device's own list (`node.yaml`
      `games`, `nox node games add`) and what the core added (`nox/games`, never a removal);
    * an anti-cheat process, service or kernel driver runs (`nox.desktop.anticheat`);
    * a watched game or an anti-cheat process is in front, or the foreground cannot be read;
    * something owns the screen: a full-screen Direct3D app, "busy" or presentation mode
      (`SHQueryUserNotificationState`) - games that are on no list are caught here;
    * any check fails. Off Windows there is no input on a node at all (`windows_only`)."""

    watched: Callable[[], Iterable[str]] = default_games
    running_names: Callable[[], set[str]] | None = None
    foreground_name: Callable[[], str] = _foreground_name
    process_pids: Callable[[set[str]], set[int]] | None = None
    services: Callable[[], set[str]] = _running_services
    screen_state: Callable[[], int] = _screen_state
    windows: bool = sys.platform == "win32"

    def _names(self) -> set[str]:
        return {n.strip().lower() for n in self.watched() if n.strip()}

    def running(self) -> bool:
        """A watched game or anti-cheat software runs (True when that cannot be told)."""
        from nox.desktop.anticheat import is_anti_cheat  # noqa: PLC0415
        from nox.desktop.process_probe import running_names_on_machine  # noqa: PLC0415

        try:
            names = (self.running_names or running_names_on_machine)()
            if names & self._names() or any(is_anti_cheat(n) for n in names):
                return True
            return bool(self.services())
        except Exception:  # noqa: BLE001 - cannot tell: locked
            return True

    def in_front(self) -> bool:
        """A game is in front or owns the screen (True when that cannot be told)."""
        from nox.desktop.anticheat import LOCKING_NOTIFICATION_STATES, is_anti_cheat  # noqa: PLC0415

        try:
            name = self.foreground_name().strip().lower()
            if not name or name in self._names() or is_anti_cheat(name):
                return True  # an unreadable foreground counts as a game
            return self.screen_state() in LOCKING_NOTIFICATION_STATES
        except Exception:  # noqa: BLE001 - cannot tell: locked
            return True

    def reason(self) -> str | None:
        """`NodeGate.input_lock`: why no input (and no window change) may happen now, or None."""
        if not self.windows:
            return "windows_only"
        if self.running():
            return "game_running"
        if self.in_front():
            return "game_in_foreground"
        return None

    def pids(self) -> set[int]:
        """Process ids of the watched games and anti-cheat processes (for `desktop.*`)."""
        from nox.desktop.anticheat import ANTI_CHEAT_PROCESSES  # noqa: PLC0415

        names = self._names() | set(ANTI_CHEAT_PROCESSES)
        if self.process_pids is not None:
            return self.process_pids(names)
        import psutil  # noqa: PLC0415

        found: set[int] = set()
        for process in psutil.process_iter(["pid", "name"]):
            try:
                if str(process.info.get("name") or "").lower() in names:
                    found.add(int(process.info["pid"]))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return found

"""When the pet keeps quiet: hidden over a full-screen game, slower while any game runs.

Pure policy, no Qt. The shell observes the machine (`observe`), decides (`decide`) and tells the
pet window what to do; the page itself only draws. Everything here is read from the machine and the
configuration on each look - nothing is remembered, so a game that ends is never "still running".

What counts as a game is the same as everywhere else in Nox, without a second list: a program on
`sensors.game.process_names`, anti-cheat software, or something that owns the whole screen
(`nox.desktop.anticheat`). Observation only - process names and the shell's own notification state.

Keeping out of the way is a courtesy, not a safety lock, so a check that cannot be read counts as
"no game": it never hides the pet or slows it down on a guess.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from nox.core.config.games import DEFAULT_GAME_PROCESSES
from nox.desktop.anticheat import LOCKING_NOTIFICATION_STATES, fullscreen_state, is_anti_cheat
from nox.desktop.process_probe import running_names_on_machine
from nox.shell.logutil import get_logger

log = get_logger(__name__)

__all__ = [
    "DEFAULT_FPS_IN_GAME",
    "DEFAULT_FPS_TARGET",
    "FPS_MAX",
    "Observed",
    "Presence",
    "QuietSettings",
    "decide",
    "frames_message",
    "observe",
    "watched_games",
]

DEFAULT_FPS_TARGET = 60
DEFAULT_FPS_IN_GAME = 20
FPS_MIN = 1
FPS_MAX = 240


def _bounded_fps(value: object, default: int) -> int:
    """A whole frame rate between 1 and 240; the default for anything else (a hand-edited file)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value if FPS_MIN <= value <= FPS_MAX else default


@dataclass(frozen=True, slots=True)
class QuietSettings:
    """The `pet.*` settings that decide how the pet behaves around games."""

    hide_in_fullscreen: bool = True
    fps_target: int = DEFAULT_FPS_TARGET
    fps_in_game: int = DEFAULT_FPS_IN_GAME

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> QuietSettings:
        """Read from the shell's configuration mapping; anything missing or malformed keeps its
        default, because the shell must start whatever the file says."""
        pet = config.get("pet")
        if not isinstance(pet, Mapping):
            return cls()
        hide = pet.get("hide_in_fullscreen", True)
        return cls(
            hide_in_fullscreen=hide if isinstance(hide, bool) else True,
            fps_target=_bounded_fps(pet.get("fps_target"), DEFAULT_FPS_TARGET),
            fps_in_game=_bounded_fps(pet.get("fps_in_game"), DEFAULT_FPS_IN_GAME),
        )


@dataclass(frozen=True, slots=True)
class Observed:
    """What the machine looked like at one moment."""

    game_running: bool = False
    fullscreen: bool = False


@dataclass(frozen=True, slots=True)
class Presence:
    """What the pet should do: whether it steps aside, and whether it runs at the in-game rate."""

    hidden: bool
    game_active: bool


def decide(settings: QuietSettings, observed: Observed) -> Presence:
    """Hidden only while something owns the screen and the user wants that; throttled while a game
    runs or owns the screen."""
    return Presence(
        hidden=settings.hide_in_fullscreen and observed.fullscreen,
        game_active=observed.game_running or observed.fullscreen,
    )


def frames_message(settings: QuietSettings, presence: Presence) -> dict[str, Any]:
    """What the pet page is told about its frame rate (`window` event `nox-pet-frames`)."""
    return {
        "fpsTarget": settings.fps_target,
        "fpsInGame": settings.fps_in_game,
        "gameActive": presence.game_active,
    }


def watched_games(config: Mapping[str, Any]) -> tuple[str, ...]:
    """`sensors.game.process_names` from the shell's configuration, else the shipped default."""
    sensors = config.get("sensors")
    game = sensors.get("game") if isinstance(sensors, Mapping) else None
    names = game.get("process_names") if isinstance(game, Mapping) else None
    if isinstance(names, list) and all(isinstance(n, str) for n in names):
        return tuple(names)
    return DEFAULT_GAME_PROCESSES


def observe(
    watched: Iterable[str],
    *,
    running_names: Callable[[], set[str]] = running_names_on_machine,
    screen_state: Callable[[], int] = fullscreen_state,
) -> Observed:
    """Look at the machine once. Both reads may be slow (the process list): call it off the UI
    thread. A read that fails is logged and counts as "nothing to see"."""
    wanted = {name.strip().lower() for name in watched if name.strip()}
    try:
        names = running_names()
        game_running = bool(names & wanted) or any(is_anti_cheat(n) for n in names)
    except Exception as exc:  # noqa: BLE001 - a courtesy check: never let it stop the pet
        log.debug("pet_quiet.process_list_unreadable", error=type(exc).__name__)
        game_running = False
    try:
        fullscreen = screen_state() in LOCKING_NOTIFICATION_STATES
    except Exception as exc:  # noqa: BLE001 - not Windows, or the shell would not say
        log.debug("pet_quiet.screen_state_unreadable", error=type(exc).__name__)
        fullscreen = False
    return Observed(game_running=game_running, fullscreen=fullscreen)

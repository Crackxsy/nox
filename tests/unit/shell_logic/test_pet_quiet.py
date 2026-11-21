"""When the pet keeps quiet around games: pure policy, a fake machine, no window."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from nox.core.config.games import DEFAULT_GAME_PROCESSES
from nox.shell.pet_quiet import (
    DEFAULT_FPS_IN_GAME,
    DEFAULT_FPS_TARGET,
    Observed,
    Presence,
    QuietSettings,
    decide,
    frames_message,
    observe,
    watched_games,
)

# `SHQueryUserNotificationState`: not busy / busy / full-screen Direct3D / presentation / store app.
FREE, BUSY, FULLSCREEN, PRESENTATION, APP = 5, 2, 3, 4, 7


@pytest.mark.parametrize(
    ("settings", "observed", "hidden", "game_active"),
    [
        (QuietSettings(), Observed(), False, False),
        (QuietSettings(), Observed(game_running=True), False, True),
        (QuietSettings(), Observed(fullscreen=True), True, True),
        (QuietSettings(), Observed(game_running=True, fullscreen=True), True, True),
        (QuietSettings(hide_in_fullscreen=False), Observed(fullscreen=True), False, True),
        (QuietSettings(hide_in_fullscreen=False), Observed(game_running=True), False, True),
        (QuietSettings(hide_in_fullscreen=False), Observed(), False, False),
    ],
)
def test_the_pet_hides_over_full_screen_and_slows_down_around_any_game(
    settings: QuietSettings, observed: Observed, hidden: bool, game_active: bool
) -> None:
    assert decide(settings, observed) == Presence(hidden=hidden, game_active=game_active)


def test_the_page_is_told_the_frame_budget_and_whether_a_game_is_active() -> None:
    settings = QuietSettings(fps_target=75, fps_in_game=12)
    assert frames_message(settings, Presence(hidden=False, game_active=True)) == {
        "fpsTarget": 75,
        "fpsInGame": 12,
        "gameActive": True,
    }
    quiet = frames_message(settings, Presence(hidden=False, game_active=False))
    assert quiet["gameActive"] is False


def test_the_settings_default_to_the_documented_values() -> None:
    settings = QuietSettings.from_config({})
    assert settings == QuietSettings(True, DEFAULT_FPS_TARGET, DEFAULT_FPS_IN_GAME)
    assert (settings.fps_target, settings.fps_in_game, settings.hide_in_fullscreen) == (
        60,
        20,
        True,
    )


def test_the_settings_are_read_from_the_pet_section() -> None:
    config = {"pet": {"hide_in_fullscreen": False, "fps_target": 144, "fps_in_game": 10}}
    assert QuietSettings.from_config(config) == QuietSettings(False, 144, 10)


@pytest.mark.parametrize(
    "pet",
    [
        {"fps_target": 0, "fps_in_game": 241},
        {"fps_target": "fast", "fps_in_game": None},
        {"fps_target": True, "fps_in_game": 12.5},
        {"fps_target": -1, "fps_in_game": [20]},
        {"hide_in_fullscreen": "yes"},
        {"hide_in_fullscreen": 0},
        "not a mapping",
        None,
        [],
    ],
)
def test_a_malformed_setting_keeps_its_default(pet: Any) -> None:
    assert QuietSettings.from_config({"pet": pet}) == QuietSettings()


def test_the_watched_list_comes_from_the_configuration_or_the_shipped_default() -> None:
    configured = {"sensors": {"game": {"process_names": ["A.exe", "B.exe"]}}}
    assert watched_games(configured) == ("A.exe", "B.exe")
    assert watched_games({}) == DEFAULT_GAME_PROCESSES
    assert watched_games({"sensors": {"game": {"process_names": "A.exe"}}}) == (
        DEFAULT_GAME_PROCESSES
    )
    assert watched_games({"sensors": {"game": {"process_names": [1, 2]}}}) == (
        DEFAULT_GAME_PROCESSES
    )
    assert watched_games({"sensors": None}) == DEFAULT_GAME_PROCESSES


def _screen(state: int) -> Callable[[], int]:
    return lambda: state


@pytest.mark.parametrize(
    ("names", "screen", "expected"),
    [
        (set(), FREE, Observed(False, False)),
        ({"explorer.exe"}, FREE, Observed(False, False)),
        ({"examplegame.exe"}, FREE, Observed(True, False)),
        ({"easyanticheat.exe"}, FREE, Observed(True, False)),
        ({"explorer.exe"}, FULLSCREEN, Observed(False, True)),
        ({"explorer.exe"}, BUSY, Observed(False, True)),
        ({"explorer.exe"}, PRESENTATION, Observed(False, True)),
        ({"explorer.exe"}, APP, Observed(False, False)),
        ({"examplegame.exe"}, FULLSCREEN, Observed(True, True)),
    ],
)
def test_observe_reads_processes_and_the_screen(
    names: set[str], screen: int, expected: Observed
) -> None:
    seen = observe(["ExampleGame.exe"], running_names=lambda: names, screen_state=_screen(screen))
    assert seen == expected


def test_observe_ignores_blank_entries_on_the_watched_list() -> None:
    seen = observe(["", "  "], running_names=lambda: {""}, screen_state=_screen(FREE))
    assert seen == Observed()


def _boom() -> Any:
    raise OSError("cannot be read")


def test_a_check_that_cannot_be_read_never_hides_or_slows_the_pet() -> None:
    """Keeping out of the way is a courtesy: an unreadable machine counts as 'nothing to see'."""
    assert observe(["x.exe"], running_names=_boom, screen_state=_screen(FREE)) == Observed()
    unreadable_screen = observe(["x.exe"], running_names=lambda: {"x.exe"}, screen_state=_boom)
    assert unreadable_screen == Observed(game_running=True, fullscreen=False)

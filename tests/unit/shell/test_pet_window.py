"""PetWindow: shared off-the-record QWebEngineProfile, hardened settings."""

from __future__ import annotations

from typing import Any

from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEngineSettings

from nox.shell.pet_layout import Monitor, Rect
from nox.shell.pet_window import PetWindow, _pet_profile
from nox.shell.runtime import ShellState


def test_profile_is_off_the_record_and_hardened(qapp: Any) -> None:
    profile = _pet_profile()
    assert profile.isOffTheRecord() is True
    assert profile.isSpellCheckEnabled() is False
    assert (
        profile.persistentCookiesPolicy()
        == QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies
    )
    settings = profile.settings()
    assert settings.testAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled) is False
    assert settings.testAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled) is False
    assert settings.testAttribute(QWebEngineSettings.WebAttribute.ScreenCaptureEnabled) is False
    assert settings.testAttribute(QWebEngineSettings.WebAttribute.ShowScrollBars) is False


def test_pet_windows_share_the_same_profile(qapp: Any) -> None:
    w1 = PetWindow(ShellState())
    w2 = PetWindow(ShellState())
    assert w1.view.page().profile() is w2.view.page().profile()
    assert w1.view.page().profile() is _pet_profile()
    assert w1.view.settings().testAttribute(QWebEngineSettings.WebAttribute.ShowScrollBars) is False


# ---- placement and quiet -------------------------------------------------------------------------

LEFT = Monitor(Rect(0, 0, 1920, 1040), primary=True)
RIGHT = Monitor(Rect(1920, 0, 2560, 1400))


def test_a_new_pet_window_opens_on_the_default_monitor(qapp: Any) -> None:
    window = PetWindow(ShellState(), default_monitor="right", monitors=lambda: [LEFT, RIGHT])
    assert RIGHT.area.overlap_area(Rect(window.x(), window.y(), window.width(), window.height()))
    assert window.x() >= RIGHT.area.x


def test_a_saved_position_still_on_a_screen_is_kept(qapp: Any) -> None:
    window = PetWindow(ShellState(x=100, y=120), monitors=lambda: [LEFT, RIGHT])
    assert (window.x(), window.y()) == (100, 120)


def test_a_saved_position_on_an_unplugged_monitor_moves_to_the_default_monitor(qapp: Any) -> None:
    window = PetWindow(ShellState(x=7000, y=100), default_monitor="left", monitors=lambda: [LEFT])
    assert LEFT.area.overlap_area(Rect(window.x(), window.y(), window.width(), window.height()))


def test_a_window_that_ends_up_off_screen_comes_back_and_is_saved(qapp: Any) -> None:
    screens = [LEFT, RIGHT]
    moved: list[tuple[int, int]] = []
    window = PetWindow(
        ShellState(x=100, y=100),
        on_moved=lambda x, y: moved.append((x, y)),
        monitors=lambda: screens,
    )
    assert window.keep_visible() is False and moved == []
    screens.remove(RIGHT)
    window.move(RIGHT.area.x + 200, 100)  # it was on the monitor that has just gone
    assert window.keep_visible() is True
    assert LEFT.area.overlap_area(Rect(window.x(), window.y(), window.width(), window.height()))
    assert moved == [(window.x(), window.y())]


def test_the_page_is_told_its_frame_budget_once_per_change_and_after_every_load(
    qapp: Any, monkeypatch: Any
) -> None:
    window = PetWindow(ShellState(), monitors=lambda: [LEFT])
    pushed: list[str | None] = []
    monkeypatch.setattr(window, "_push_frames", lambda: pushed.append(window.frames_script()))
    assert window.frames_script() is None
    message = {"fpsTarget": 60, "fpsInGame": 20, "gameActive": True}
    window.set_frames(message)
    window.set_frames(dict(message))  # unchanged: nothing sent again
    assert len(pushed) == 1
    script = pushed[0]
    assert script is not None
    assert '{"fpsTarget": 60, "fpsInGame": 20, "gameActive": true}' in script
    assert "nox-pet-frames" in script
    window._on_load_finished(True)  # a reloaded page starts from its defaults
    window._on_load_finished(False)  # a failed load has nothing to tell
    assert len(pushed) == 2


def test_the_frame_script_is_data_only(qapp: Any) -> None:
    window = PetWindow(ShellState(), monitors=lambda: [LEFT])
    window.set_frames({"fpsTarget": 60, "fpsInGame": 20, "gameActive": False})
    script = window.frames_script() or ""
    assert script.startswith("window.__noxPetFrames=")
    assert script.count(";") == 2

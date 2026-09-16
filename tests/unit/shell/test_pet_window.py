"""PetWindow: shared off-the-record QWebEngineProfile, hardened settings (B-7, ADR-004)."""

from __future__ import annotations

from typing import Any

from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEngineSettings

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

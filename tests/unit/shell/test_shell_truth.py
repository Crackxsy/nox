"""The shell does what the user set up: the hotkeys from the user's configuration, a stuck
push-to-talk released, a refused one explained, the tray right after a reconnect, and a pet that
is never restored onto a screen that no longer exists (offscreen Qt, fake bridge)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nox.core.state import PrivacyMode, SystemLevel
from nox.shell.app import ShellApp
from nox.shell.hotkeys import GlobalHotkeys, HotkeyTracker
from nox.shell.logic import (
    PET_SCREEN_MARGIN_PX,
    HotkeyAction,
    TrayTint,
    hotkey_map,
    hotkey_map_or_defaults,
    parse_hotkey,
    pet_position,
    ptt_refusal_text,
    tray_tint,
)
from nox.shell.pet_window import PetWindow
from nox.shell.runtime import ShellState, load_config
from tests.unit.shell.conftest import FakeBridge
from tests.unit.shell.test_app import CONFIG, make_runtime

STATUS = {
    "mode": "private",
    "capture": {"microphone": False, "camera": False, "screen": False, "cloud": False},
    "zone_active": True,
    "panic": False,
    "safe_mode": False,
    "muted": True,
}


def bridge_factory(responses: dict[str, Any]) -> tuple[Any, list[FakeBridge]]:
    created: list[FakeBridge] = []

    def factory(url: str, token: str, role: str, client_id: str) -> FakeBridge:
        bridge = FakeBridge(url, token, role, client_id)
        bridge.responses = dict(responses)
        created.append(bridge)
        return bridge

    return factory, created


def make_app(
    tmp_path: Path, factory: Any, *, config: dict[str, Any] | None = None, **kw: Any
) -> ShellApp:
    return ShellApp(
        runtime_dir=make_runtime(tmp_path),
        config=config if config is not None else CONFIG,
        bridge_factory=factory,
        create_pet_window=False,
        open_url=lambda _url: None,
        **{"enable_hotkeys": False, **kw},
    )


# ---- the user's configuration --------------------------------------------------------------


def test_the_shell_reads_the_users_hotkey_not_only_the_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("NOX_CONFIG", raising=False)
    user = tmp_path / "user.yaml"
    user.write_text("voice:\n  stt:\n    push_to_talk_hotkey: ctrl+shift+f9\n", encoding="utf-8")
    monkeypatch.setenv("NOX_USER_CONFIG", str(user))
    config = load_config()
    assert config["voice"]["stt"]["push_to_talk_hotkey"] == "ctrl+shift+f9"
    assert hotkey_map(config)[parse_hotkey("ctrl+shift+f9")] is HotkeyAction.PTT


def test_a_user_layer_the_core_would_reject_is_rejected_here_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("NOX_CONFIG", raising=False)
    user = tmp_path / "user.yaml"
    user.write_text("voice:\n  stt:\n    no_such_key: 1\n", encoding="utf-8")
    monkeypatch.setenv("NOX_USER_CONFIG", str(user))
    assert load_config()["voice"]["stt"]["push_to_talk_hotkey"] == "ctrl+alt+space"


def test_a_hotkey_edited_in_the_dashboard_is_applied_without_a_restart(
    qapp: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, _ = bridge_factory({})
    app = make_app(tmp_path, factory, enable_hotkeys=True)
    app._config_from_disk = True
    edited = {"voice": {"stt": {"push_to_talk_hotkey": "ctrl+shift+f9"}}}
    monkeypatch.setattr("nox.shell.app.load_config", lambda: edited)
    app.handle_event(
        {"name": "settings.changed", "payload": {"paths": ["voice.stt.push_to_talk_hotkey"]}}
    )
    assert app._hotkeys is not None
    tracker: HotkeyTracker = app._hotkeys._tracker
    assert tracker.mapping[parse_hotkey("ctrl+shift+f9")] is HotkeyAction.PTT
    assert parse_hotkey("ctrl+alt+space") not in tracker.mapping


def test_conflicting_hotkeys_fall_back_to_the_defaults_instead_of_crashing(
    qapp: Any, tmp_path: Path
) -> None:
    config = {"voice": {"stt": {"push_to_talk_hotkey": "ctrl+alt+m"}}}  # the mute combo
    mapping, reason = hotkey_map_or_defaults(config)
    assert "conflict" in reason
    assert mapping == hotkey_map(None)
    factory, _ = bridge_factory({})
    notes: list[str] = []
    app = make_app(tmp_path, factory, config=config)  # used to raise ValueError here
    app.tray.notify = lambda _t, text, **_k: notes.append(text)  # type: ignore[method-assign]
    app.start()
    assert any("Konflikt" in n for n in notes)
    app.quit()


# ---- stuck and refused push-to-talk --------------------------------------------------------


def test_reset_releases_a_ptt_whose_key_up_was_lost() -> None:
    tracker = HotkeyTracker(hotkey_map({}))
    for key in ("ctrl_l", "alt_l", "space"):
        tracker.press(key)
    # Win+L while holding: the key-ups never arrive.
    assert tracker.reset() == [(HotkeyAction.PTT, False)]
    assert tracker.pressed_keys == frozenset()
    # A stale Ctrl can no longer turn Alt+Shift+K into the kill switch.
    for key in ("alt_l", "shift_l"):
        tracker.press(key)
    assert tracker.press("k") == []


def test_ctrl_letters_arrive_as_control_characters_and_still_match() -> None:
    key = SimpleNamespace(name=None, char="\x0b", vk=0x4B)  # Ctrl+K as pynput reports it
    assert GlobalHotkeys._key_name(key) == "k"
    assert GlobalHotkeys._key_name(SimpleNamespace(name=None, char="\x0b", vk=None)) == "k"
    assert GlobalHotkeys._key_name(SimpleNamespace(name=None, char="k", vk=0x4B)) == "k"


def test_an_auto_released_ptt_resets_the_hotkey_listener(qapp: Any, tmp_path: Path) -> None:
    factory, created = bridge_factory({})
    app = make_app(tmp_path, factory, enable_hotkeys=True)
    app.start()
    qapp.processEvents()
    assert app._hotkeys is not None
    for key in ("ctrl_l", "alt_l", "space"):
        app._hotkeys._on_press(SimpleNamespace(name=key, char=None, vk=None))
    qapp.processEvents()
    assert ("voice.ptt", {"pressed": True}) in created[0].calls
    app.handle_event({"name": "voice.ptt_released", "payload": {"reason": "max_hold"}})
    qapp.processEvents()
    assert app._hotkeys._tracker.pressed_keys == frozenset()
    assert created[0].calls[-1] == ("voice.ptt", {"pressed": False})
    app.quit()


def test_a_refused_ptt_is_told_to_the_user(qapp: Any, tmp_path: Path) -> None:
    factory, _ = bridge_factory({})
    app = make_app(tmp_path, factory, config={"identity": {"ui_language": "de"}})
    notes: list[str] = []
    app.tray.notify = lambda _t, text, **_k: notes.append(text)  # type: ignore[method-assign]
    app.handle_event(
        {"name": "voice.ptt_refused", "payload": {"reason": "privacy_zone", "zone": "banking"}}
    )
    assert notes == ["Push-to-Talk: Mikrofon bleibt aus: Datenschutzzone „banking“ ist aktiv."]


@pytest.mark.parametrize(
    "reason",
    ["privacy_zone", "microphone_off", "muted", "panic", "safe_mode", "voice_unavailable", "other"],
)
def test_every_refusal_reason_has_words_in_both_languages(reason: str) -> None:
    for language in ("de", "en"):
        text = ptt_refusal_text({"reason": reason, "zone": "email"}, language)
        assert text and "{" not in text


# ---- the tray after a (re)connect ----------------------------------------------------------


def test_the_tray_starts_from_the_cores_state_not_from_defaults(qapp: Any, tmp_path: Path) -> None:
    factory, created = bridge_factory({"privacy.status": STATUS})
    app = make_app(tmp_path, factory)
    app.start()
    qapp.processEvents()
    assert ("privacy.status", {}) in created[0].calls
    assert app.model.muted is True
    assert app.model.privacy_mode is PrivacyMode.PRIVATE
    assert tray_tint(app.model) is TrayTint.MUTED
    app.quit()


def test_the_tray_is_resynced_after_a_reconnect(qapp: Any, tmp_path: Path) -> None:
    factory, created = bridge_factory({"privacy.status": {**STATUS, "safe_mode": True}})
    app = make_app(tmp_path, factory)
    app.start()
    qapp.processEvents()
    assert app.model.system_level is SystemLevel.SAFE_MODE
    created[0].fail_calls = True  # the core restarts
    app._ping()
    app._ping()
    qapp.processEvents()
    assert app.bridge is None and not app.model.connected
    app._retry_bridge()  # a fresh core, no longer in safe mode
    created[-1].responses["privacy.status"] = {**STATUS, "safe_mode": False, "muted": False}
    app._ping()
    qapp.processEvents()
    assert app.model.connected
    assert app.model.system_level is SystemLevel.RUNNING
    assert app.model.muted is False
    app.quit()


# ---- the pet stays on a screen that exists -------------------------------------------------

PRIMARY = (0, 0, 1920, 1040)
RIGHT = (1920, 0, 2560, 1400)


def test_a_saved_position_on_an_existing_screen_is_kept() -> None:
    assert pet_position((2400, 300), (260, 300), [PRIMARY, RIGHT], PRIMARY) == (2400, 300)


def test_a_position_on_an_unplugged_monitor_falls_back_to_the_primary_screen() -> None:
    x, y = pet_position((2400, 300), (260, 300), [PRIMARY], PRIMARY)
    assert (x, y) == (1920 - 260 - PET_SCREEN_MARGIN_PX, 1040 - 300 - PET_SCREEN_MARGIN_PX)


def test_a_pet_barely_touching_a_screen_edge_counts_as_off_screen() -> None:
    assert pet_position((1900, 300), (260, 300), [PRIMARY], PRIMARY) != (1900, 300)


def test_the_pet_window_never_opens_off_screen(qapp: Any) -> None:
    window = PetWindow(ShellState(x=-50_000, y=-50_000))
    screen = qapp.primaryScreen().availableGeometry()
    assert screen.contains(window.pos())

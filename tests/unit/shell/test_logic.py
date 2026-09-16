from __future__ import annotations

import pytest

from nox.core.state import PrivacyMode, SystemLevel
from nox.shell.logic import (
    DEFAULT_HOTKEYS,
    CaptureFlags,
    HotkeyAction,
    ShellModel,
    TrayTint,
    dashboard_url,
    hotkey_map,
    kill_path,
    parse_hotkey,
    permission_reply,
    pet_url,
    toggle_privacy,
    tray_tint,
    tray_tooltip,
)


def test_tray_tint_priority() -> None:
    m = ShellModel()
    assert tray_tint(m) == TrayTint.OFFLINE
    m.connected = True
    assert tray_tint(m) == TrayTint.NORMAL
    m.privacy_mode = PrivacyMode.PRIVATE
    assert tray_tint(m) == TrayTint.PRIVATE
    m.muted = True
    assert tray_tint(m) == TrayTint.MUTED
    m.capture = CaptureFlags(cloud=True)
    assert tray_tint(m) == TrayTint.CLOUD
    m.capture = CaptureFlags(microphone=True, cloud=True)
    assert tray_tint(m) == TrayTint.CAPTURING
    m.system_level = SystemLevel.SAFE_MODE
    assert tray_tint(m) == TrayTint.SAFE_MODE
    m.connected = False
    assert tray_tint(m) == TrayTint.OFFLINE


def test_capture_event_changes_tint_and_tooltip() -> None:
    m = ShellModel(connected=True)
    changed = m.apply_event(
        "privacy.capture_changed",
        {"microphone": True, "camera": False, "screen": True, "cloud": False},
    )
    assert changed == {"capture"}
    assert tray_tint(m) == TrayTint.CAPTURING
    tip = tray_tooltip(m, "de")
    assert "microphone" in tip and "screen" in tip and "camera" not in tip
    assert "Aufnahme" in tip
    assert "capture" in tray_tooltip(m, "en")


def test_disconnect_clears_capture_flags() -> None:
    m = ShellModel(connected=True, capture=CaptureFlags(screen=True))
    assert m.set_connected(False) is True
    assert m.capture.any_local is False
    assert m.set_connected(False) is False


def test_privacy_mode_event_and_state_changed() -> None:
    m = ShellModel(connected=True)
    m.apply_event(
        "privacy.mode_changed", {"previous": "balanced", "current": "private", "by": "user"}
    )
    assert m.privacy_mode == PrivacyMode.PRIVATE
    assert m.previous_normal_privacy == PrivacyMode.BALANCED
    m.apply_event(
        "state.changed", {"path": "privacy.mode", "old": "private", "new": "full", "version": 3}
    )
    assert m.privacy_mode == PrivacyMode.FULL
    m.apply_event(
        "state.changed", {"path": "privacy.microphone", "old": False, "new": True, "version": 4}
    )
    assert m.capture.microphone is True
    m.apply_event(
        "state.changed", {"path": "assistant.muted", "old": False, "new": True, "version": 5}
    )
    assert m.muted is True
    m.apply_event(
        "state.changed",
        {"path": "system.level", "old": "running", "new": "safe_mode", "version": 6},
    )
    assert m.system_level == SystemLevel.SAFE_MODE


def test_kill_switch_event_sets_safe_mode() -> None:
    m = ShellModel(connected=True)
    assert m.apply_event("security.kill_switch", {"by": "hotkey"}) == {"system_level"}
    assert m.apply_event("system.started", {}) == {"system_level"}
    assert m.apply_event("unknown.event", {"x": 1}) == set()


def test_toggle_privacy_round_trip() -> None:
    m = ShellModel(privacy_mode=PrivacyMode.FULL)
    assert toggle_privacy(m) == PrivacyMode.PRIVATE
    m.set_privacy(PrivacyMode.PRIVATE)
    assert toggle_privacy(m) == PrivacyMode.FULL
    m.set_privacy(PrivacyMode.OFFLINE)
    assert toggle_privacy(m) == PrivacyMode.FULL


def test_kill_path() -> None:
    assert kill_path(ShellModel(connected=True)) == "core"
    assert kill_path(ShellModel(connected=False)) == "supervisor"


def test_parse_hotkey_normalises() -> None:
    assert parse_hotkey("Ctrl + Alt + Space") == frozenset({"ctrl", "alt", "space"})
    assert parse_hotkey("strg+shift+K") == frozenset({"ctrl", "shift", "k"})
    with pytest.raises(ValueError):
        parse_hotkey("+")


def test_hotkey_map_from_config_and_defaults() -> None:
    cfg = {
        "voice": {"stt": {"push_to_talk_hotkey": "ctrl+alt+space"}},
        "supervisor": {"kill_switch_hotkey": "ctrl+alt+shift+k"},
        "shell": {"hotkeys": {"mute": "ctrl+alt+u"}},
    }
    mapping = hotkey_map(cfg)
    assert mapping[frozenset({"ctrl", "alt", "space"})] == HotkeyAction.PTT
    assert mapping[frozenset({"ctrl", "alt", "shift", "k"})] == HotkeyAction.KILL
    assert mapping[frozenset({"ctrl", "alt", "u"})] == HotkeyAction.MUTE
    assert mapping[parse_hotkey(DEFAULT_HOTKEYS["privacy"])] == HotkeyAction.PRIVACY
    assert len(mapping) == 5


def test_hotkey_map_rejects_conflicts() -> None:
    with pytest.raises(ValueError, match="conflict"):
        hotkey_map({"shell": {"hotkeys": {"mute": "ctrl+alt+space"}}})


def test_permission_reply_payload() -> None:
    req = {
        "request_id": "r1",
        "agent": "coder",
        "tool": "fs",
        "action": "write",
        "mode": "coding",
        "risk": "medium",
    }
    assert permission_reply(req, allow=True, remember=True) == {
        "grant_id": "r1",
        "decision": "allow",
        "remember": True,
    }
    assert permission_reply(req, allow=False, remember=False)["decision"] == "deny"
    with pytest.raises(ValueError):
        permission_reply({"agent": "x"}, allow=True, remember=False)


def test_urls_keep_token_out_of_query() -> None:
    assert dashboard_url(47801, "tok") == "http://127.0.0.1:47801/dashboard/#token=tok"
    assert pet_url(47801, "tok") == "http://127.0.0.1:47801/pet/#token=tok"
    assert pet_url(47801, "tok", overlay=True) == "http://127.0.0.1:47801/pet/?overlay=1#token=tok"
    # OP-1: variant is a plain, non-secret query flag; "neutral" (the default) is omitted so
    # existing/unconfigured setups keep producing the same URL as before.
    assert pet_url(47801, "tok", variant="neutral") == "http://127.0.0.1:47801/pet/#token=tok"
    assert (
        pet_url(47801, "tok", variant="fox") == "http://127.0.0.1:47801/pet/?variant=fox#token=tok"
    )
    assert (
        pet_url(47801, "tok", overlay=True, variant="fox")
        == "http://127.0.0.1:47801/pet/?overlay=1&variant=fox#token=tok"
    )
    for url in (
        dashboard_url(1, "tok"),
        pet_url(1, "tok", overlay=True),
        pet_url(1, "tok", overlay=True, variant="fox"),
    ):
        query = url.split("?", 1)[1].split("#", 1)[0] if "?" in url else ""
        assert "tok" not in query

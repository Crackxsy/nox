from __future__ import annotations

from nox.shell.hotkeys import HotkeyTracker, normalise_key
from nox.shell.logic import HotkeyAction, hotkey_map


def make() -> HotkeyTracker:
    return HotkeyTracker(hotkey_map({}))


def test_ptt_press_and_release() -> None:
    t = make()
    assert t.press("ctrl_l") == []
    assert t.press("alt_l") == []
    assert t.press("space") == [(HotkeyAction.PTT, True)]
    assert t.press("space") == []  # auto-repeat does not re-fire
    assert t.release("space") == [(HotkeyAction.PTT, False)]
    assert t.release("ctrl_l") == []
    assert t.release("alt_l") == []
    assert t.pressed_keys == frozenset()


def test_ptt_releases_when_modifier_released_first() -> None:
    t = make()
    for k in ("ctrl_l", "alt_l", "space"):
        t.press(k)
    assert t.release("ctrl_l") == [(HotkeyAction.PTT, False)]
    assert t.release("space") == []


def test_kill_switch_fires_once_on_press_only() -> None:
    t = make()
    for k in ("ctrl_r", "alt_r", "shift_l"):
        assert t.press(k) == []
    assert t.press("k") == [(HotkeyAction.KILL, True)]
    assert t.release("k") == []
    assert t.press("k") == [(HotkeyAction.KILL, True)]


def test_partial_combo_does_not_fire_other_actions() -> None:
    t = make()
    t.press("ctrl_l")
    t.press("alt_l")
    assert t.press("m") == [(HotkeyAction.MUTE, True)]
    assert t.press("p") == [(HotkeyAction.PRIVACY, True)]  # ctrl+alt+p still satisfied
    t.release("m")
    t.release("p")
    assert t.press("x") == []


def test_reset_clears_stuck_keys() -> None:
    t = make()
    for k in ("ctrl_l", "alt_l", "space"):
        t.press(k)
    t.reset()
    assert t.pressed_keys == frozenset()
    assert t.press("space") == []


def test_normalise_key() -> None:
    assert normalise_key("Ctrl_L") == "ctrl"
    assert normalise_key("alt_gr") == "alt"
    assert normalise_key("K") == "k"
    assert normalise_key(None) is None
    assert normalise_key("") is None

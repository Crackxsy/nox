"""Global hotkeys via pynput (D239): PTT press/release, mute, privacy, kill switch, show/hide pet.

`HotkeyTracker` is pure (testable): it turns key press/release streams into actions. `GlobalHotkeys`
wraps a pynput listener thread and forwards to a callback; callers must marshal into the Qt thread.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nox.shell.logic import HotkeyAction
from nox.shell.logutil import get_logger

log = get_logger(__name__)

HotkeyCallback = Callable[[HotkeyAction, bool], None]  # (action, pressed)

_PYNPUT_NAMES: dict[str, str] = {
    "ctrl_l": "ctrl",
    "ctrl_r": "ctrl",
    "alt_l": "alt",
    "alt_r": "alt",
    "alt_gr": "alt",
    "shift_l": "shift",
    "shift_r": "shift",
    "cmd_l": "cmd",
    "cmd_r": "cmd",
}


def normalise_key(name: str | None) -> str | None:
    if not name:
        return None
    key = name.lower()
    return _PYNPUT_NAMES.get(key, key)


class HotkeyTracker:
    """Holds the set of currently pressed keys and matches combos.

    A combo fires on the press that completes it (`pressed=True`). For actions that care about
    release (PTT) a `pressed=False` follows as soon as any key of the active combo is released.
    Other actions only receive `pressed=True`; the release is swallowed.
    """

    def __init__(self, mapping: dict[frozenset[str], HotkeyAction]) -> None:
        self._mapping = mapping
        self._down: set[str] = set()
        self._active: dict[HotkeyAction, frozenset[str]] = {}

    @property
    def pressed_keys(self) -> frozenset[str]:
        return frozenset(self._down)

    def press(self, key: str | None) -> list[tuple[HotkeyAction, bool]]:
        norm = normalise_key(key)
        if norm is None:
            return []
        self._down.add(norm)
        fired: list[tuple[HotkeyAction, bool]] = []
        for combo, action in self._mapping.items():
            if action in self._active:
                continue
            if combo <= self._down and norm in combo:
                self._active[action] = combo
                fired.append((action, True))
        return fired

    def release(self, key: str | None) -> list[tuple[HotkeyAction, bool]]:
        norm = normalise_key(key)
        if norm is None:
            return []
        self._down.discard(norm)
        fired: list[tuple[HotkeyAction, bool]] = []
        for action, combo in list(self._active.items()):
            if norm in combo:
                del self._active[action]
                if action == HotkeyAction.PTT:
                    fired.append((action, False))
        return fired

    def reset(self) -> None:
        """Forget everything (e.g. after focus loss so PTT cannot stick)."""
        released = [(a, False) for a in self._active if a == HotkeyAction.PTT]
        self._down.clear()
        self._active.clear()
        if released:
            log.debug("hotkeys.reset_released_ptt")


class GlobalHotkeys:
    """pynput keyboard listener feeding a HotkeyTracker. Runs on pynput's own thread."""

    def __init__(self, tracker: HotkeyTracker, callback: HotkeyCallback) -> None:
        self._tracker = tracker
        self._callback = callback
        self._listener: Any = None

    def start(self) -> bool:
        try:
            from pynput import keyboard
        except ImportError:
            log.warning("hotkeys.pynput_missing")
            return False
        self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self._listener.daemon = True
        self._listener.start()
        log.info("hotkeys.started")
        return True

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

    @staticmethod
    def _key_name(key: Any) -> str | None:
        name = getattr(key, "name", None)
        if name:
            return str(name)
        char = getattr(key, "char", None)
        if char:
            return str(char)
        vk = getattr(key, "vk", None)
        if isinstance(vk, int) and 0x30 <= vk <= 0x5A:  # digits/letters while modifiers are held
            return chr(vk).lower()
        return None

    def _on_press(self, key: Any) -> None:
        for action, pressed in self._tracker.press(self._key_name(key)):
            self._safe_callback(action, pressed)

    def _on_release(self, key: Any) -> None:
        for action, pressed in self._tracker.release(self._key_name(key)):
            self._safe_callback(action, pressed)

    def _safe_callback(self, action: HotkeyAction, pressed: bool) -> None:
        try:
            self._callback(action, pressed)
        except Exception:  # never let a UI error kill the listener thread
            log.exception("hotkeys.callback_failed", action=str(action))

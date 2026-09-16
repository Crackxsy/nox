"""Windows extended window styles for click-through (WS_EX_TRANSPARENT), ctypes only. FR-4.4."""

from __future__ import annotations

import ctypes
import sys

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020


def click_through_style(style: int, enabled: bool) -> int:
    """Pure bit math so it can be unit-tested anywhere: keeps WS_EX_LAYERED, toggles TRANSPARENT."""
    style |= WS_EX_LAYERED
    return style | WS_EX_TRANSPARENT if enabled else style & ~WS_EX_TRANSPARENT


def set_click_through(hwnd: int, enabled: bool) -> bool:
    """Apply to a real window. Returns False on non-Windows platforms (feature unavailable)."""
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    style = int(user32.GetWindowLongW(hwnd, GWL_EXSTYLE))
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, click_through_style(style, enabled))
    return True


def is_click_through(hwnd: int) -> bool:
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    return bool(int(user32.GetWindowLongW(hwnd, GWL_EXSTYLE)) & WS_EX_TRANSPARENT)

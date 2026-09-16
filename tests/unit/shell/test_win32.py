from __future__ import annotations

from nox.shell.win32 import WS_EX_LAYERED, WS_EX_TRANSPARENT, click_through_style


def test_click_through_style_bits() -> None:
    base = 0x00000100
    on = click_through_style(base, True)
    assert on & WS_EX_TRANSPARENT and on & WS_EX_LAYERED and on & base
    off = click_through_style(on, False)
    assert not off & WS_EX_TRANSPARENT
    assert off & WS_EX_LAYERED and off & base

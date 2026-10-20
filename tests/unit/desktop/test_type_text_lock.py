"""`desktop.type_text` cannot approve Nox's own confirmations: it shares computer use's lock.

Typing with `press_enter` into a browser tab showing the dashboard, with "Allow" focused, would
answer a confirmation. Fakes only - `type_text` is replaced, nothing is ever typed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from nox.desktop.computer_use import CONFIRMATION_QUIET_S, ComputerUse
from nox.desktop.tools import register_desktop_tools
from nox.desktop.win32 import WindowInfo
from nox.tools.registry import ToolRegistry

DASHBOARD_TAB = WindowInfo(
    handle=21,
    title="Nox Dashboard - Google Chrome",
    process_name="chrome.exe",
    pid=100,
    minimized=False,
)
NOX_SHELL = WindowInfo(
    handle=22, title="Berechtigung", process_name="python.exe", pid=4242, minimized=False
)
EDITOR = WindowInfo(
    handle=23, title="notizen.txt - Editor", process_name="notepad.exe", pid=200, minimized=False
)


@dataclass
class Probe:
    windows_list: list[WindowInfo]
    focused: list[int] = field(default_factory=list)

    def windows(self) -> list[WindowInfo]:
        return list(self.windows_list)

    def focus(self, handle: int) -> bool:
        self.focused.append(handle)
        return True

    def minimize(self, handle: int) -> bool:
        return True

    def restore(self, handle: int) -> bool:
        return True

    def close(self, handle: int) -> bool:
        return True


@dataclass
class World:
    pending: bool = False
    now: float = 1000.0


def setup(
    monkeypatch: pytest.MonkeyPatch, window: WindowInfo, world: World
) -> tuple[Any, ComputerUse, Probe, list[str]]:
    typed: list[str] = []

    def fake_type(text: str, *, press_enter: bool, game_running: bool, expected_window: int) -> int:
        typed.append(text)
        return len(text)

    monkeypatch.setattr("nox.desktop.tools.type_text", fake_type)

    def no_sender() -> Any:
        raise AssertionError("typing must not create a computer-use sender")

    computer = ComputerUse(
        enabled=lambda: False,  # computer use itself switched off: the lock still holds
        game_running=lambda: False,
        game_in_foreground=lambda: False,
        nox_pids=lambda: {4242},
        sender=no_sender,
        audit=lambda *_a: None,
        clock=lambda: world.now,
        confirmation_pending=lambda: world.pending,
    )
    registry = ToolRegistry()
    probe = Probe([window])
    register_desktop_tools(registry, probe, set, input_guard=computer)
    spec = registry.get("desktop.type_text")
    assert spec is not None
    return spec.handler, computer, probe, typed


@pytest.mark.parametrize("window", [DASHBOARD_TAB, NOX_SHELL])
async def test_typing_into_a_nox_window_is_refused(
    monkeypatch: pytest.MonkeyPatch, window: WindowInfo
) -> None:
    handler, computer, probe, typed = setup(monkeypatch, window, World())
    answer = await handler({"handle": window.handle, "text": "x", "press_enter": True})
    assert answer["ok"] is False and "Nox window" in answer["error"]
    assert typed == [] and probe.focused == []  # not even brought to the front
    assert computer.blocks_confirmation() is False


async def test_nothing_is_typed_while_a_confirmation_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(pending=True)
    handler, _computer, probe, typed = setup(monkeypatch, EDITOR, world)
    answer = await handler({"handle": EDITOR.handle, "text": "hallo", "press_enter": True})
    assert answer["ok"] is False and "confirmation is waiting" in answer["error"]
    assert typed == [] and probe.focused == []


async def test_typing_arms_the_same_confirmation_lock_as_a_click(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World()
    handler, computer, _probe, typed = setup(monkeypatch, EDITOR, world)
    assert computer.blocks_confirmation() is False
    answer = await handler({"handle": EDITOR.handle, "text": "hallo", "press_enter": True})
    assert answer["ok"] is True and typed == ["hallo"]
    # An "allow" answered right after the keystrokes may be Nox's own Enter: refused for a while.
    assert computer.blocks_confirmation() is True
    world.now += CONFIRMATION_QUIET_S + 0.1
    assert computer.blocks_confirmation() is False


async def test_without_computer_use_a_window_naming_nox_still_gets_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    typed: list[str] = []
    monkeypatch.setattr(
        "nox.desktop.tools.type_text", lambda text, **_kw: typed.append(text) or len(text)
    )
    registry = ToolRegistry()
    register_desktop_tools(registry, Probe([DASHBOARD_TAB]), set)
    spec = registry.get("desktop.type_text")
    assert spec is not None
    answer = await spec.handler({"handle": DASHBOARD_TAB.handle, "text": "x"})
    assert answer["ok"] is False and typed == []

"""The window tools, and the case that matters most: several windows match.

"Close Chrome" with four Chrome windows open must not pick one. A guess closes the wrong window
and the user cannot tell a guess was made, so the answer is the candidates and nothing else.
"""

from __future__ import annotations

from typing import Any

import pytest

from nox.desktop.tools import register_desktop_tools
from nox.desktop.win32 import WindowInfo
from nox.security.model import Risk
from nox.tools.registry import ToolRegistry

CHROME_A = WindowInfo(
    handle=11, title="Nox - Google Chrome", process_name="chrome.exe", pid=100, minimized=False
)
CHROME_B = WindowInfo(
    handle=12, title="Wetter - Google Chrome", process_name="chrome.exe", pid=100, minimized=True
)
EDITOR = WindowInfo(
    handle=13, title="notizen.txt - Editor", process_name="notepad.exe", pid=200, minimized=False
)
GAME = WindowInfo(
    handle=14, title="Rocket League", process_name="RocketLeague.exe", pid=300, minimized=False
)


class FakeProbe:
    """Records what it was asked to do, so "did nothing" is an assertable outcome."""

    def __init__(self, windows: list[WindowInfo], works: bool = True) -> None:
        self._windows = windows
        self.works = works
        self.focused: list[int] = []
        self.closed: list[int] = []
        self.minimized: list[int] = []
        self.restored: list[int] = []

    def windows(self) -> list[WindowInfo]:
        return list(self._windows)

    def focus(self, handle: int) -> bool:
        self.focused.append(handle)
        return self.works

    def minimize(self, handle: int) -> bool:
        self.minimized.append(handle)
        return self.works

    def restore(self, handle: int) -> bool:
        self.restored.append(handle)
        return self.works

    def close(self, handle: int) -> bool:
        self.closed.append(handle)
        return self.works


def build(
    windows: list[WindowInfo] | None = None, *, games: set[int] | None = None, works: bool = True
) -> tuple[dict[str, Any], FakeProbe]:
    registry = ToolRegistry()
    probe = FakeProbe(windows if windows is not None else [CHROME_A, EDITOR], works=works)
    register_desktop_tools(registry, probe, lambda: games or set())
    return {name: registry.get(name).handler for name in registry.names()}, probe


async def test_listing_windows_reports_what_is_open() -> None:
    handlers, _ = build()

    answer = await handlers["desktop.windows"]({})

    assert [window["title"] for window in answer["windows"]] == [CHROME_A.title, EDITOR.title]
    assert answer["windows"][1]["process"] == "notepad.exe"


async def test_the_game_is_listed_and_marked() -> None:
    """Knowing the game is open is observation. Acting on it is what the boundary refuses."""
    handlers, _ = build([CHROME_A, GAME], games={GAME.pid})

    answer = await handlers["desktop.windows"]({})

    assert [window["is_game"] for window in answer["windows"]] == [False, True]


async def test_a_title_fragment_finds_one_window() -> None:
    handlers, probe = build()

    answer = await handlers["desktop.window_focus"]({"title": "notizen"})

    assert answer["ok"] and probe.focused == [EDITOR.handle]


async def test_the_program_name_also_matches() -> None:
    handlers, probe = build()

    answer = await handlers["desktop.window_minimize"]({"title": "notepad"})

    assert answer["ok"] and probe.minimized == [EDITOR.handle]


async def test_several_matches_change_nothing_and_say_so() -> None:
    handlers, probe = build([CHROME_A, CHROME_B, EDITOR])

    answer = await handlers["desktop.window_close"]({"title": "chrome"})

    assert answer["ok"] is False and "2 windows match" in answer["error"]
    assert [entry["handle"] for entry in answer["candidates"]] == [11, 12]
    assert probe.closed == [], "an ambiguous request must not close anything"


async def test_a_handle_from_the_listing_removes_the_ambiguity() -> None:
    handlers, probe = build([CHROME_A, CHROME_B, EDITOR])

    answer = await handlers["desktop.window_close"]({"handle": 12})

    assert answer["ok"] and probe.closed == [12]


async def test_nothing_matching_is_an_answer_not_a_crash() -> None:
    handlers, probe = build()

    answer = await handlers["desktop.window_focus"]({"title": "photoshop"})

    assert answer["ok"] is False and "no open window matches" in answer["error"]
    assert probe.focused == []


async def test_a_window_tool_refuses_the_game() -> None:
    handlers, probe = build([GAME], games={GAME.pid})

    answer = await handlers["desktop.window_close"]({"title": "rocket"})

    assert answer["ok"] is False and "observed only" in answer["error"]
    assert probe.closed == []


async def test_focusing_reports_when_windows_refused() -> None:
    """`SetForegroundWindow` fails for reasons this code cannot influence; saying it worked lies."""
    handlers, _ = build(works=False)

    answer = await handlers["desktop.window_focus"]({"title": "notizen"})

    assert answer["ok"] is False and "did not bring" in answer["error"]


async def test_a_reference_has_to_be_one_thing_or_the_other() -> None:
    handlers, _ = build()

    with pytest.raises(ValueError, match="either a title fragment or a handle"):
        await handlers["desktop.window_focus"]({})
    with pytest.raises(ValueError, match="either a title fragment or a handle"):
        await handlers["desktop.window_focus"]({"title": "x", "handle": 11})


def test_the_risk_levels_say_what_each_tool_costs() -> None:
    """Closing asks the program; stopping kills it. The levels have to reflect that difference."""
    registry = ToolRegistry()
    register_desktop_tools(registry, FakeProbe([]), set)

    assert registry.get("desktop.windows").risk is Risk.READ
    assert registry.get("desktop.processes").risk is Risk.READ
    assert registry.get("desktop.window_focus").risk is Risk.LOW
    assert registry.get("desktop.window_close").risk is Risk.MEDIUM
    assert registry.get("desktop.process_stop").risk is Risk.HIGH

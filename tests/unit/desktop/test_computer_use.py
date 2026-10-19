"""`nox.desktop.computer_use`: every guard refuses, with a recording sender instead of Windows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from nox.core.config.hands import ComputerUseConfig
from nox.desktop.computer_tools import register_computer_tools
from nox.desktop.computer_use import (
    KEYS,
    ComputerUse,
    ComputerUseError,
    GameRunningError,
    parse_combo,
)
from nox.security.model import Risk
from nox.tools.registry import ToolRegistry


@dataclass
class Recorder:
    events: list[tuple[Any, ...]] = field(default_factory=list)
    stop: bool = False
    front: tuple[int, str] = (100, "Editor")
    at: tuple[int, str] = (100, "Editor")
    #: Called before each event is recorded - lets a test start a game mid-action.
    before_event: Any = None

    def screen(self) -> tuple[int, int, int, int]:
        return (0, 0, 1920, 1080)

    def window_at(self, x: int, y: int) -> tuple[int, str]:
        return self.at

    def foreground(self) -> tuple[int, str]:
        return self.front

    def stop_pressed(self) -> bool:
        return self.stop

    def mouse(self, dx: int, dy: int, flags: int, data: int = 0) -> None:
        self.events.append(("mouse", dx, dy, flags, data))

    def key(self, vk: int, *, up: bool) -> None:
        self.events.append(("key", vk, up))


@dataclass
class World:
    enabled: bool = True
    game: bool = False
    game_front: bool = False
    now: float = 1000.0
    audit: list[tuple[str, str, str]] = field(default_factory=list)
    notices: list[tuple[bool, str]] = field(default_factory=list)


def make(world: World, recorder: Recorder) -> ComputerUse:
    return ComputerUse(
        enabled=lambda: world.enabled,
        game_running=lambda: world.game,
        game_in_foreground=lambda: world.game_front,
        nox_pids=lambda: {4242},
        sender=lambda: recorder,
        audit=lambda a, t, r: world.audit.append((a, t, r)),
        notice=lambda active, reason: world.notices.append((active, reason)),
        clock=lambda: world.now,
    )


def test_off_by_default() -> None:
    assert ComputerUseConfig().enabled is False


def test_no_session_while_disabled_or_while_a_game_runs() -> None:
    world, rec = World(enabled=False), Recorder()
    cu = make(world, rec)
    with pytest.raises(ComputerUseError, match="switched off"):
        cu.start_session(minutes=5, max_actions=10)
    world.enabled, world.game = True, True
    with pytest.raises(GameRunningError):
        cu.start_session(minutes=5, max_actions=10)
    assert rec.events == []


def test_nothing_is_sent_without_a_session() -> None:
    world, rec = World(), Recorder()
    with pytest.raises(ComputerUseError, match="no computer-use session"):
        make(world, rec).pointer("click", 10, 10)
    assert rec.events == []


def test_a_click_inside_a_session_moves_then_clicks_and_is_audited() -> None:
    world, rec = World(), Recorder()
    cu = make(world, rec)
    cu.start_session(minutes=5, max_actions=10)
    cu.pointer("click", 959, 539)
    kinds = [e[3] for e in rec.events]
    assert kinds[0] & 0x8000 and kinds[1:] == [0x0002, 0x0004]
    assert ("pointer.click", "959,539", "ok") in world.audit
    assert world.notices == [(True, "for 5 minutes")]


def test_a_game_starting_mid_action_stops_before_the_next_event() -> None:
    world, rec = World(), Recorder()
    cu = make(world, rec)
    cu.start_session(minutes=5, max_actions=10)
    original = rec.mouse

    def mouse(*args: Any) -> None:
        original(*args)
        world.game = True  # the game starts right after the first event

    rec.mouse = mouse  # type: ignore[method-assign]
    with pytest.raises(GameRunningError):
        cu.pointer("click", 10, 10)
    assert len(rec.events) == 1  # the move went out; the click never did
    assert not cu.active and world.notices[-1] == (False, "a game started")


def test_a_game_in_the_foreground_blocks_every_event() -> None:
    world, rec = World(), Recorder()
    cu = make(world, rec)
    cu.start_session(minutes=5, max_actions=10)
    world.game_front = True
    with pytest.raises(GameRunningError, match="foreground"):
        cu.keys("ctrl+s")
    with pytest.raises(GameRunningError):
        cu.pointer("scroll", amount=-2)
    assert rec.events == []


@pytest.mark.parametrize(
    "combo", ["win+r", "Win+X", "ctrl+alt+del", "ctrl+shift+esc", "strg+alt+shift+k"]
)
def test_blocked_combinations_are_never_sent(combo: str) -> None:
    world, rec = World(), Recorder()
    cu = make(world, rec)
    cu.start_session(minutes=5, max_actions=10)
    with pytest.raises(ComputerUseError, match="never sent"):
        cu.keys(combo)
    assert rec.events == []


def test_alt_f4_and_clicks_on_nox_itself_are_refused() -> None:
    world, rec = World(), Recorder(front=(4242, "Nox"), at=(4242, "Nox - Bestaetigung"))
    cu = make(world, rec)
    cu.start_session(minutes=5, max_actions=10)
    with pytest.raises(ComputerUseError, match="does not close itself"):
        cu.keys("alt+f4")
    with pytest.raises(ComputerUseError, match="Nox"):
        cu.keys("enter")
    with pytest.raises(ComputerUseError, match="does not click itself"):
        cu.pointer("click", 100, 100)
    rec.front = rec.at = (1, "Nox Dashboard - Browser")  # the dashboard in a browser tab
    with pytest.raises(ComputerUseError):
        cu.pointer("click", 100, 100)
    assert rec.events == []


def test_a_combination_is_pressed_and_fully_released() -> None:
    world, rec = World(), Recorder()
    cu = make(world, rec)
    cu.start_session(minutes=5, max_actions=10)
    assert cu.keys("Ctrl+Shift+S") == "ctrl+shift+s"
    assert rec.events == [
        ("key", 0x11, False),
        ("key", 0x10, False),
        ("key", KEYS["s"], False),
        ("key", KEYS["s"], True),
        ("key", 0x10, True),
        ("key", 0x11, True),
    ]
    assert KEYS["s"] == 0x53


def test_the_stop_hotkey_ends_the_session_before_anything_is_sent() -> None:
    world, rec = World(), Recorder(stop=True)
    cu = make(world, rec)
    cu.start_session(minutes=5, max_actions=10)
    with pytest.raises(ComputerUseError, match="hotkey"):
        cu.pointer("click", 1, 1)
    assert rec.events == [] and not cu.active


def test_sessions_end_by_time_and_by_action_budget() -> None:
    world, rec = World(), Recorder()
    cu = make(world, rec)
    cu.start_session(minutes=1, max_actions=2)
    cu.keys("tab")
    cu.keys("tab")
    with pytest.raises(ComputerUseError):
        cu.keys("tab")
    cu.start_session(minutes=1, max_actions=50)
    world.now += 61
    with pytest.raises(ComputerUseError):
        cu.keys("tab")


def test_switching_it_off_ends_a_running_session() -> None:
    world, rec = World(), Recorder()
    cu = make(world, rec)
    cu.start_session(minutes=5, max_actions=10)
    world.enabled = False
    with pytest.raises(ComputerUseError, match="switched off"):
        cu.keys("tab")
    assert not cu.active


@pytest.mark.parametrize("combo", ["ctrl+", "ctrl+ctrl+s", "foo+s", "ctrl+alt+shift+win+x", "xyz"])
def test_bad_combinations(combo: str) -> None:
    with pytest.raises(ComputerUseError):
        parse_combo(combo)


def test_points_off_screen_are_refused() -> None:
    world, rec = World(), Recorder()
    cu = make(world, rec)
    cu.start_session(minutes=5, max_actions=10)
    with pytest.raises(ComputerUseError, match="outside"):
        cu.pointer("click", 5000, 10)


# ---- the tools -----------------------------------------------------------------------------------


async def test_tools_grant_once_per_session_and_revoke_at_its_end() -> None:
    world, rec = World(), Recorder()
    cu = make(world, rec)
    registry = ToolRegistry()
    grants: list[int] = []
    revoked: list[str] = []
    unremembered: list[bool] = []
    end = register_computer_tools(
        registry,
        cu,
        lambda: ComputerUseConfig(enabled=True),
        grant=lambda minutes: grants.append(minutes) or f"g{len(grants)}",
        revoke=revoked.append,
        unremember=lambda: unremembered.append(True),
    )
    start, pointer = registry.get("computer.session_start"), registry.get("computer.pointer")
    assert start is not None and start.risk is Risk.HIGH
    assert pointer is not None and pointer.risk is Risk.MEDIUM
    answer = await start.handler({"purpose": "Datei speichern"})
    assert answer["ok"] and grants == [15] and unremembered == [True]
    assert (await pointer.handler({"action": "click", "x": 5, "y": 5}))["ok"]
    end("kill switch")
    assert revoked == ["g1"] and not cu.active
    refused = await pointer.handler({"action": "click", "x": 5, "y": 5})
    assert refused["ok"] is False


async def test_a_refused_session_start_grants_nothing() -> None:
    world, rec = World(game=True), Recorder()
    registry = ToolRegistry()
    grants: list[int] = []
    register_computer_tools(
        registry,
        make(world, rec),
        lambda: ComputerUseConfig(enabled=True),
        grant=lambda minutes: grants.append(minutes) or "g",
        revoke=lambda _g: None,
    )
    start = registry.get("computer.session_start")
    assert start is not None
    answer = await start.handler({"purpose": "Datei speichern"})
    assert answer["ok"] is False and grants == []

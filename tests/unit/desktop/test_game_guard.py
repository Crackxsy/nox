"""The core's game guard locks input for the same reasons a node does, and says why.

Every case is a fake machine: no process list, service manager or window is read."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import product
from typing import Any

import pytest

from nox.desktop.computer_use import ComputerUse, ComputerUseError, GameRunningError
from nox.desktop.game_guard import CODE_IN_FRONT, CODE_RUNNING, GameGuard
from nox.node.device.world import GameLock

#: `SHQueryUserNotificationState`: not busy / full-screen Direct3D / presentation / busy / app.
FREE, BUSY, FULLSCREEN, PRESENTATION, APP = 5, 2, 3, 4, 7


@dataclass
class Machine:
    names: set[str] = field(default_factory=set)
    services: set[str] = field(default_factory=set)
    front: str = "explorer.exe"
    screen: int = FREE
    watched: list[str] = field(default_factory=lambda: ["ExampleGame.exe"])

    def guard(self) -> GameGuard:
        return GameGuard(
            watched=lambda: self.watched,
            running_names=lambda: self.names,
            foreground_name=lambda: self.front,
            services=lambda: self.services,
            screen_state=lambda: self.screen,
        )

    def node_lock(self) -> GameLock:
        return GameLock(
            watched=lambda: self.watched,
            running_names=lambda: self.names,
            foreground_name=lambda: self.front,
            services=lambda: self.services,
            screen_state=lambda: self.screen,
            windows=True,
        )


def _raise(message: str = "cannot be read") -> Callable[[], Any]:
    def fail() -> Any:
        raise OSError(message)

    return fail


@pytest.mark.parametrize(
    ("machine", "code"),
    [
        pytest.param(Machine(), None, id="nothing-is-going-on"),
        pytest.param(Machine(names={"examplegame.exe"}), CODE_RUNNING, id="watched-process"),
        pytest.param(Machine(names={"easyanticheat.exe"}), CODE_RUNNING, id="anti-cheat-process"),
        pytest.param(Machine(names={"gamemon64.des"}), CODE_RUNNING, id="anti-cheat-family"),
        pytest.param(Machine(services={"vgk"}), CODE_RUNNING, id="anti-cheat-driver"),
        pytest.param(Machine(front="examplegame.exe"), CODE_IN_FRONT, id="watched-in-front"),
        pytest.param(Machine(front="beservice.exe"), CODE_IN_FRONT, id="anti-cheat-in-front"),
        pytest.param(Machine(front=""), CODE_IN_FRONT, id="front-unreadable"),
        pytest.param(Machine(screen=FULLSCREEN), CODE_IN_FRONT, id="fullscreen-without-a-list"),
        pytest.param(Machine(screen=BUSY), CODE_IN_FRONT, id="busy"),
        pytest.param(Machine(screen=PRESENTATION), CODE_IN_FRONT, id="presentation"),
        pytest.param(Machine(screen=APP), None, id="a-store-app-in-front-is-no-game"),
        pytest.param(
            Machine(names={"examplegame.exe"}, screen=FULLSCREEN), CODE_RUNNING, id="running-first"
        ),
    ],
)
def test_the_guard_locks_for_each_way_a_game_shows_itself(
    machine: Machine, code: str | None
) -> None:
    guard = machine.guard()
    assert guard.reason() == code
    assert (guard.lock() is None) == (code is None)
    assert guard.running() == (code == CODE_RUNNING)


def test_in_front_is_answered_on_its_own() -> None:
    assert Machine(front="examplegame.exe").guard().in_front()
    assert Machine(screen=FULLSCREEN).guard().in_front()
    assert not Machine(names={"examplegame.exe"}).guard().in_front()
    assert not Machine().guard().in_front()


def test_a_game_that_is_on_no_list_and_has_no_anti_cheat_is_caught_by_the_screen() -> None:
    """A full-screen game nobody listed (a sandbox game, say) must still lock input."""
    machine = Machine(names={"someunlistedgame.exe"}, screen=FULLSCREEN)
    state = machine.guard().lock()
    assert state is not None
    assert state.code == CODE_IN_FRONT
    assert "full-screen" in state.cause


@pytest.mark.parametrize(
    ("machine", "cause"),
    [
        (Machine(names={"examplegame.exe"}), "watched list"),
        (Machine(names={"easyanticheat.exe"}), "anti-cheat software"),
        (Machine(services={"vgk"}), "service or driver"),
        (Machine(front="examplegame.exe"), "a game is in front"),
        (Machine(front=""), "could not be identified"),
        (Machine(screen=PRESENTATION), "owns the screen"),
    ],
)
def test_the_cause_is_a_sentence_a_person_can_read(machine: Machine, cause: str) -> None:
    assert cause in machine.guard().cause()


def test_the_cause_is_empty_while_input_is_free() -> None:
    assert Machine().guard().cause() == ""


@pytest.mark.parametrize("broken", ["running_names", "services", "foreground_name", "screen_state"])
def test_every_check_that_cannot_answer_locks(broken: str) -> None:
    guard = Machine().guard()
    setattr(guard, broken, _raise())
    assert guard.reason() is not None
    assert guard.cause() != ""


def test_the_watched_list_is_read_on_every_question() -> None:
    machine = Machine(names={"newgame.exe"})
    guard = machine.guard()
    assert guard.reason() is None
    machine.watched.append("NewGame.exe")
    assert guard.reason() == CODE_RUNNING


def test_watched_names_match_without_case_or_padding() -> None:
    machine = Machine(names={"examplegame.exe"}, watched=["  ExampleGAME.exe "])
    assert machine.guard().reason() == CODE_RUNNING
    assert Machine(watched=["", "  "], names={""}).guard().running() is False


MACHINES = [
    Machine(names=set(names), services=set(services), front=front, screen=screen)
    for names, services, front, screen in product(
        [(), ("examplegame.exe",), ("pnkbstrb.exe",), ("notepad.exe",)],
        [(), ("EasyAntiCheat",)],
        ["explorer.exe", "examplegame.exe", "xigncode.exe", ""],
        [FREE, FULLSCREEN, BUSY, APP],
    )
]


@pytest.mark.parametrize("machine", MACHINES)
def test_the_core_and_a_node_reach_the_same_verdict(machine: Machine) -> None:
    """The two guards are separate code; this holds them to one answer on every combination."""
    assert machine.guard().reason() == machine.node_lock().reason()


# ---- the guard in front of the computer-use module ----------------------------------------------


@dataclass
class Hands:
    sent: list[str] = field(default_factory=list)

    def screen(self) -> tuple[int, int, int, int]:
        return (0, 0, 1920, 1080)

    def window_at(self, x: int, y: int) -> tuple[Any, ...]:
        return (100, "Editor", "Notepad")

    def foreground(self) -> tuple[Any, ...]:
        return (100, "Editor", "Notepad")

    def cursor(self) -> tuple[int, int]:
        return (500, 500)

    def stop_pressed(self) -> bool:
        return False

    def mouse(self, dx: int, dy: int, flags: int, data: int = 0) -> None:
        self.sent.append("mouse")

    def key(self, vk: int, *, up: bool) -> None:
        self.sent.append("key")


def _computer(machine: Machine, hands: Hands) -> ComputerUse:
    guard = machine.guard()
    return ComputerUse(
        enabled=lambda: True,
        game_running=guard.running,
        game_in_foreground=guard.in_front,
        nox_pids=lambda: {4242},
        sender=lambda: hands,
        audit=lambda *_args: None,
        lock_cause=guard.cause,
    )


def test_a_session_is_refused_and_the_user_learns_why() -> None:
    computer = _computer(Machine(services={"vgk"}), Hands())
    with pytest.raises(GameRunningError, match=r"service or driver"):
        computer.start_session(minutes=5, max_actions=10)


def test_a_full_screen_game_stops_a_running_session_before_the_next_event() -> None:
    machine = Machine()
    hands = Hands()
    computer = _computer(machine, hands)
    computer.start_session(minutes=5, max_actions=10)
    machine.screen = FULLSCREEN
    with pytest.raises(ComputerUseError, match=r"full-screen"):
        computer.keys("ctrl+s")
    assert hands.sent == []


def test_typing_is_refused_while_a_game_runs_or_is_in_front() -> None:
    machine = Machine()
    computer = _computer(machine, Hands())
    assert computer.typing_refusal(100, "Editor") is None
    machine.names = {"examplegame.exe"}
    assert "game" in (computer.typing_refusal(100, "Editor") or "")
    machine.names = set()
    machine.screen = FULLSCREEN
    refusal = computer.typing_refusal(100, "Editor")
    assert refusal is not None and "full-screen" in refusal

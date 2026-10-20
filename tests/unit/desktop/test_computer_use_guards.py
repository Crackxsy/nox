"""Computer use cannot approve itself: per-event window checks, no moves onto Nox, and a core that
refuses "allow" and privileged UI requests while Nox could be the one clicking. Fakes only - no
real mouse or keyboard event is ever sent by these tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import BaseModel

from nox.desktop.computer_use import (
    CONFIRMATION_QUIET_S,
    ComputerUse,
    ComputerUseError,
)
from nox.ipc.dispatch import RequestContext, RequestRegistry
from nox.ipc.protocol import Envelope, Kind, Source

NOX = (4242, "Nox - Berechtigung", "Qt6110QWindowIcon")
EDITOR = (100, "Editor", "Notepad")


@dataclass
class Recorder:
    events: list[tuple[Any, ...]] = field(default_factory=list)
    front: tuple[Any, ...] = EDITOR
    under_cursor: tuple[Any, ...] = EDITOR
    at_target: tuple[Any, ...] = EDITOR
    #: Called after each event is recorded - lets a test open a Nox dialog mid-action.
    after_event: Any = None

    def screen(self) -> tuple[int, int, int, int]:
        return (0, 0, 1920, 1080)

    def window_at(self, x: int, y: int) -> tuple[Any, ...]:
        return self.under_cursor if (x, y) == (500, 500) else self.at_target

    def foreground(self) -> tuple[Any, ...]:
        return self.front

    def cursor(self) -> tuple[int, int]:
        return (500, 500)

    def stop_pressed(self) -> bool:
        return False

    def mouse(self, dx: int, dy: int, flags: int, data: int = 0) -> None:
        self.events.append(("mouse", flags))
        if self.after_event:
            self.after_event()

    def key(self, vk: int, *, up: bool) -> None:
        self.events.append(("key", vk, up))
        if self.after_event:
            self.after_event()


@dataclass
class World:
    game: bool = False
    game_front: bool = False
    pending: bool = False
    now: float = 1000.0


def make(world: World, rec: Recorder) -> ComputerUse:
    cu = ComputerUse(
        enabled=lambda: True,
        game_running=lambda: world.game,
        game_in_foreground=lambda: world.game_front,
        nox_pids=lambda: {4242},
        sender=lambda: rec,
        audit=lambda *_a: None,
        clock=lambda: world.now,
        confirmation_pending=lambda: world.pending,
    )
    cu.start_session(minutes=5, max_actions=50)
    return cu


def test_a_click_without_coordinates_on_an_unfocused_nox_dialog_is_refused() -> None:
    # The dialog is visible under the pointer, but an editor has the focus: checking only the
    # foreground window let this click through.
    world, rec = World(), Recorder(under_cursor=NOX)
    cu = make(world, rec)
    with pytest.raises(ComputerUseError, match="pointer is on a Nox window"):
        cu.pointer("click")
    assert rec.events == []


def test_moving_onto_a_nox_window_is_refused() -> None:
    world, rec = World(), Recorder(at_target=NOX)
    cu = make(world, rec)
    with pytest.raises(ComputerUseError, match="Nox window"):
        cu.pointer("move", 300, 300)
    assert rec.events == []


def test_a_nox_window_appearing_between_down_and_up_stops_the_up() -> None:
    world, rec = World(), Recorder()
    cu = make(world, rec)

    def dialog_opens() -> None:
        rec.under_cursor = NOX

    rec.after_event = dialog_opens
    with pytest.raises(ComputerUseError):
        cu.pointer("click")
    assert len(rec.events) == 1  # the down went out before the dialog; the up never did


def test_nothing_is_sent_while_a_confirmation_waits() -> None:
    world, rec = World(pending=True), Recorder()
    cu = make(world, rec)
    with pytest.raises(ComputerUseError, match="confirmation"):
        cu.keys("enter")
    with pytest.raises(ComputerUseError, match="confirmation"):
        cu.pointer("click")
    assert rec.events == []


def test_key_ups_are_not_sent_into_a_game_that_came_to_the_front() -> None:
    world, rec = World(), Recorder()
    cu = make(world, rec)

    def game_comes_up() -> None:
        world.game_front = True

    rec.after_event = game_comes_up
    with pytest.raises(ComputerUseError):
        cu.keys("ctrl+s")
    assert rec.events == [("key", 0x11, False)]  # one down, and no key-up into the game
    assert not cu.active


@pytest.mark.parametrize(
    "combo", ["win+s", "win+e", "win+d", "win+1", "ctrl+esc", "alt+tab", "alt+shift+tab", "win"]
)
def test_launching_and_switching_combinations_are_refused(combo: str) -> None:
    world, rec = World(), Recorder()
    cu = make(world, rec)
    with pytest.raises(ComputerUseError):
        cu.keys(combo)
    assert rec.events == []


def test_confirmation_stays_blocked_shortly_after_the_last_event() -> None:
    world, rec = World(), Recorder()
    cu = make(world, rec)
    cu.keys("tab")
    assert cu.blocks_confirmation()
    cu.end_session("stop hotkey")
    assert cu.blocks_confirmation()  # an event Nox sent a moment ago may still be on its way
    world.now += CONFIRMATION_QUIET_S + 0.1
    assert not cu.blocks_confirmation()


class Empty(BaseModel):
    pass


async def _dispatch(registry: RequestRegistry, name: str, role: str = "dashboard") -> Envelope:
    request = Envelope(kind=Kind.REQUEST, name=name, src=Source(role=role, id=f"{role}:1"))
    return await registry.dispatch(RequestContext(client_id=f"{role}:1", role=role,
                                                  request=request), request)  # fmt: skip


async def test_privileged_ui_requests_are_refused_while_nox_could_be_clicking() -> None:
    registry = RequestRegistry()

    async def handler(_ctx: RequestContext, _p: Empty) -> dict[str, Any]:
        return {"ok": True}

    for name in ("skills.activate", "skills.list", "security.kill", "mcp.servers.approve"):
        registry.register(name, Empty, handler, roles=("dashboard", "shell"))
    locked = [True]
    registry.input_lock = lambda: locked[0]
    for name in ("skills.activate", "mcp.servers.approve"):
        reply = await _dispatch(registry, name)
        assert reply.kind is Kind.ERROR and "stop hotkey" in reply.payload["message"], name
    for name in ("skills.list", "security.kill"):  # looking and stopping still work
        assert (await _dispatch(registry, name)).kind is Kind.RESPONSE, name
    locked[0] = False
    assert (await _dispatch(registry, "skills.activate")).kind is Kind.RESPONSE


async def test_a_paired_device_is_locked_out_too_while_nox_could_be_clicking() -> None:
    # A paired device is a browser as well - possibly on the very machine Nox is steering.
    registry = RequestRegistry()

    async def handler(_ctx: RequestContext, _p: Empty) -> dict[str, Any]:
        return {"ok": True}

    registry.register("plans.approve", Empty, handler, roles=("dashboard", "shell", "device"))
    registry.register("plans.list", Empty, handler, roles=("dashboard", "shell", "device"))
    registry.input_lock = lambda: True
    reply = await _dispatch(registry, "plans.approve", role="device")
    assert reply.kind is Kind.ERROR and "stop hotkey" in reply.payload["message"]
    assert (await _dispatch(registry, "plans.list", role="device")).kind is Kind.RESPONSE


async def test_an_allow_answer_does_not_count_while_nox_could_be_clicking() -> None:
    from types import SimpleNamespace

    from nox.ipc.handlers.core import CoreHandlers, PermissionReply
    from nox.security.model import Decision

    replies: list[Decision] = []
    engine = SimpleNamespace(reply=lambda _gid, decision, **_kw: replies.append(decision) or True)
    registry = RequestRegistry()
    locked = [True]
    registry.input_lock = lambda: locked[0]
    core = SimpleNamespace(registry=registry, security=SimpleNamespace(engine=engine))
    handlers = CoreHandlers(core)  # type: ignore[arg-type]
    ctx = SimpleNamespace(role="dashboard", client_id="dashboard:1")
    allow = PermissionReply(grant_id="g", decision=Decision.ALLOW)
    deny = PermissionReply(grant_id="g", decision=Decision.DENY)
    assert (await handlers.permission_reply(ctx, allow))["ok"] is False  # type: ignore[arg-type]
    assert (await handlers.permission_reply(ctx, deny))["ok"] is True  # type: ignore[arg-type]
    locked[0] = False
    assert (await handlers.permission_reply(ctx, allow))["ok"] is True  # type: ignore[arg-type]
    assert replies == [Decision.DENY, Decision.ALLOW]

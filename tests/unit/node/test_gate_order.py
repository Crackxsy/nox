"""The order of the gate's checks. The first refusal a call meets is the one the core hears
and the one the audit trail records, so the order is part of the contract: it is pinned
here against `nox.node.gate.CHECK_ORDER` and the module's documentation."""

from __future__ import annotations

from pathlib import Path

from nox.node.gate import INPUT_TOOLS, LOCKED_TOOLS
from tests.unit.node.support_sessions import device_world, world_gate

# ---- M1: the names and the order of the gate's checks --------------------------------------------


def test_the_locked_tools_are_exactly_these() -> None:
    assert (
        frozenset(
            {"computer.session_start", "computer.pointer", "computer.keys", "desktop.type_text"}
        )
        == INPUT_TOOLS
    )
    assert LOCKED_TOOLS == INPUT_TOOLS | {
        "desktop.window_focus",
        "desktop.window_minimize",
        "desktop.window_restore",
        "desktop.window_close",
        "desktop.process_stop",
    }


def test_the_game_lock_answers_before_the_stop_and_the_receipt(tmp_path: Path) -> None:
    world, _ = device_world(game="RocketLeague.exe")
    gate, _signer = world_gate(world, tmp_path)
    gate.stop_state = lambda: "stopped_by_core"
    assert gate.check("computer.keys", {"combo": "ctrl+s"}).reason == "game_running"  # no receipt
    gate.stop_state = lambda: None
    assert gate.check("desktop.window_close", {"title": "x"}).reason == "game_running"


def test_listing_windows_stays_allowed_during_a_game(tmp_path: Path) -> None:
    world, _ = device_world(game="RocketLeague.exe")
    gate, _signer = world_gate(world, tmp_path)
    assert gate.check("desktop.windows", {}).allowed
    assert gate.check("desktop.processes", {}).allowed

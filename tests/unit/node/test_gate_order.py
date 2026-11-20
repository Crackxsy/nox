"""The order of the gate's checks. The first refusal a call meets is the one the core hears
and the one the audit trail records, so the order is part of the contract: it is pinned
here against `nox.node.device.gate.CHECK_ORDER` and the module's documentation."""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
from pathlib import Path

from nox.node.device import gate as gate_module
from nox.node.device.gate import CHECK_ORDER, INPUT_TOOLS, LOCKED_TOOLS, NodeGate
from tests.unit.node.support_sessions import device_world, world_gate

# ---- the order itself: one list, held against the code and the documentation ---------------------

#: The checks whose refusal is a reason passed on from elsewhere: the variable `_decide` returns
#: -> the name `CHECK_ORDER` gives that check.
PASSED_ON = {"locked": "game_lock", "stopped": "stopped", "refusal": "receipt", "early": "precheck"}


def _refusals_in_code_order() -> list[str]:
    """Every `GateDecision(False, <reason>, ...)` of `NodeGate._decide`, in source order."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(NodeGate._decide)))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "GateDecision"
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value is False
    ]
    reasons: list[str] = []
    for call in sorted(calls, key=lambda node: (node.lineno, node.col_offset)):
        reason = call.args[1]
        if isinstance(reason, ast.Constant):
            name = str(reason.value)
        else:
            assert isinstance(reason, ast.Name), ast.dump(reason)
            name = PASSED_ON[reason.id]
        if not reasons or reasons[-1] != name:  # two ways to fail one check are one step
            reasons.append(name)
    return reasons


def test_the_code_runs_the_checks_in_the_documented_order() -> None:
    assert _refusals_in_code_order() == list(CHECK_ORDER)


def test_the_module_documentation_lists_the_same_order() -> None:
    documented = re.findall(r"^\s*\d+\. `([a-z_]+)`", gate_module.__doc__ or "", re.MULTILINE)
    assert documented == list(CHECK_ORDER)


def test_the_documented_steps_are_numbered_without_a_gap() -> None:
    numbers = re.findall(r"^\s*(\d+)\. `[a-z_]+`", gate_module.__doc__ or "", re.MULTILINE)
    assert [int(n) for n in numbers] == list(range(1, len(CHECK_ORDER) + 1))


# ---- which tools the game lock covers, and that it answers first --------------------------------


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

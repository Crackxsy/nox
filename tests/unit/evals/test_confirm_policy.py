"""The benchmark answers confirmation dialogs with deny unless the effect provably stays local."""

from __future__ import annotations

from pathlib import Path

import pytest

from kann_alles.confirm import decide
from nox.security.model import Decision


@pytest.fixture
def world(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "world"
    sandbox = root / "sandbox"
    sandbox.mkdir(parents=True)
    return sandbox, root


def _decide(world: tuple[Path, Path], **overrides: object) -> Decision:
    sandbox, root = world
    args: dict[str, object] = {
        "tool": "file",
        "action": "write",
        "risk": "medium",
        "target": "todo.txt",
        "confirm_tools": [r"file\.write"],
        "forbidden_tools": [],
        "sandbox": sandbox,
        "root": root,
    }
    args.update(overrides)
    return decide(**args).decision  # type: ignore[arg-type]


def test_approves_a_listed_file_write_inside_the_sandbox(world: tuple[Path, Path]) -> None:
    assert _decide(world) is Decision.ALLOW


def test_denies_a_tool_the_task_did_not_list(world: tuple[Path, Path]) -> None:
    assert _decide(world, confirm_tools=[]) is Decision.DENY
    assert _decide(world, action="move", confirm_tools=[r"file\.write"]) is Decision.DENY


def test_denies_a_file_target_outside_the_sandbox(world: tuple[Path, Path], tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere" / "x.txt"
    assert _decide(world, target=str(outside)) is Decision.DENY
    assert _decide(world, target="../../escape.txt") is Decision.DENY


def test_denies_a_file_tool_without_a_target(world: tuple[Path, Path]) -> None:
    assert _decide(world, target="") is Decision.DENY


@pytest.mark.parametrize("risk", ["high", "critical", "HIGH"])
def test_never_approves_high_or_critical_risk(world: tuple[Path, Path], risk: str) -> None:
    assert _decide(world, risk=risk) is Decision.DENY


def test_high_risk_only_in_an_isolated_world(world: tuple[Path, Path]) -> None:
    assert _decide(world, risk="high", isolated=True) is Decision.ALLOW


def test_forbidden_tools_are_denied_even_when_listed(world: tuple[Path, Path]) -> None:
    assert _decide(world, forbidden_tools=[r"file\..*"]) is Decision.DENY


def test_denies_families_that_act_outside_the_temporary_world(world: tuple[Path, Path]) -> None:
    decision = _decide(world, tool="home", action="light", target="", confirm_tools=[r"home\..*"])
    assert decision is Decision.DENY


def test_approves_data_dir_tools_without_a_path(world: tuple[Path, Path]) -> None:
    decision = _decide(
        world,
        tool="vault",
        action="append_inbox",
        target="",
        confirm_tools=[r"vault\.append_inbox"],
    )
    assert decision is Decision.ALLOW


def test_high_risk_only_when_the_task_names_it_in_approve_high(world: tuple[Path, Path]) -> None:
    learn = {"tool": "skill", "action": "learn", "risk": "high", "target": "",
             "confirm_tools": [r"skill\.learn"]}  # fmt: skip
    assert _decide(world, **learn) is Decision.DENY
    assert _decide(world, **learn, approve_high=[r"skill\.learn"]) is Decision.ALLOW
    assert _decide(world, **learn, approve_high=[r"computer\..*"]) is Decision.DENY
    critical = {**learn, "risk": "critical"}
    assert _decide(world, **critical, approve_high=[r"skill\.learn"]) is Decision.DENY


def test_fake_world_families_are_approvable_but_still_need_listing(
    world: tuple[Path, Path],
) -> None:
    mcp = {"tool": "mcp", "action": "wetter.forecast", "risk": "medium", "target": "forecast"}
    assert _decide(world, **mcp, confirm_tools=[r"mcp\.wetter\..*"]) is Decision.ALLOW
    assert _decide(world, **mcp, confirm_tools=[]) is Decision.DENY


def test_tasks_cannot_approve_other_high_risk_tools() -> None:
    from kann_alles.tasks import Task

    with pytest.raises(ValueError):
        Task.model_validate(
            {
                "id": "x-task",
                "title": "t",
                "category": "pc",
                "kind": "refuse",
                "prompt": "p",
                "approve_high": [r"package\.install"],
            }  # fmt: skip
        )

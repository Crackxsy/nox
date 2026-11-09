"""The benchmark's simulated Pi: the five Pi tasks are runnable, the core reaches the Pi through
the real router, receipts and node gate, and the harness approves a command only there."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kann_alles.confirm import decide
from kann_alles.nodes import PI_ID, PI_NAME, WorldNodes
from kann_alles.tasks import load_tasks
from nox.node.hands import ShellArgs
from nox.node.install import _shell_target
from nox.node.router import routed_spec
from nox.node.tools import NoArgs
from nox.security.model import Decision, Risk
from nox.tools.executor import CALL_DECISION, CallDecision

TASKS = Path(__file__).resolve().parents[3] / "evals" / "kann_alles" / "tasks"
PI_TASKS = {"pi-disk-space", "pi-temperature", "pi-updates", "pi-service-status", "pi-reboot"}


def test_the_five_pi_tasks_are_runnable_on_the_simulated_pi() -> None:
    tasks = {t.id: t for t in load_tasks(TASKS) if t.id in PI_TASKS}
    assert set(tasks) == PI_TASKS
    for task in tasks.values():
        assert task.needs == "" and task.env.nodes == [PI_NAME]
    # Updates and the reboot stay dialogs the harness denies.
    assert tasks["pi-updates"].approve_high == [] and tasks["pi-reboot"].approve_high == []


def _decide(tmp_path: Path, **over: Any) -> Decision:
    args: dict[str, Any] = {
        "tool": "shell",
        "action": "run",
        "risk": "high",
        "target": "Wohnzimmer-Pi: df -h",
        "confirm_tools": [r"shell\.run"],
        "forbidden_tools": [],
        "sandbox": tmp_path / "sandbox",
        "root": tmp_path,
        "approve_high": [r"shell\.run"],
        "device": PI_ID,
        "simulated_devices": frozenset({PI_ID}),
    }
    args.update(over)
    return decide(**args).decision


def test_a_command_is_approved_only_on_the_simulated_node(tmp_path: Path) -> None:
    assert _decide(tmp_path) is Decision.ALLOW
    assert _decide(tmp_path, device="") is Decision.DENY  # on this machine: never
    assert _decide(tmp_path, device="f" * 32) is Decision.DENY  # a device the world does not fake
    assert _decide(tmp_path, approve_high=[]) is Decision.DENY
    assert _decide(tmp_path, forbidden_tools=[r"shell\..*"]) is Decision.DENY


async def _routed(nodes: WorldNodes, name: str, model: Any, payload: dict[str, Any]) -> Any:
    spec = routed_spec(
        name=name,
        description="t",
        risk=Risk.HIGH if name == "shell.run" else Risk.READ,
        side_effects=name == "shell.run",
        registry=nodes,
        input_model=model,
        node_target=_shell_target if name == "shell.run" else None,
    )
    data = spec.input_model.model_validate(payload).model_dump()
    assert spec.scope is not None
    scope = spec.scope(data)
    assert scope.device == PI_ID
    return scope, await spec.handler(data)


async def test_the_core_reaches_the_simulated_pi_through_gate_and_receipts(tmp_path: Path) -> None:
    nodes = WorldNodes(tmp_path / "receipts")
    nodes.show([PI_NAME])
    _scope, info = await _routed(nodes, "system.info", NoArgs, {"device": PI_NAME})
    assert info["result"]["disk_free_gb"] == 16.2
    token = CALL_DECISION.set(CallDecision(decided_by="user"))
    try:
        payload = {"argv": ["df", "-h"], "device": PI_NAME}
        scope, ran = await _routed(nodes, "shell.run", ShellArgs, payload)
    finally:
        CALL_DECISION.reset(token)
    # Headless: the core asks (no delegation to a device without a screen) ...
    assert scope.risk is Risk.HIGH and not scope.confirm_on_device
    assert "16G" in ran["result"]["stdout"]
    # ... and a rule or a remembered answer is not a person: the Pi refuses.
    token = CALL_DECISION.set(CallDecision(decided_by="grant"))
    try:
        _scope, refused = await _routed(nodes, "shell.run", ShellArgs, payload)
    finally:
        CALL_DECISION.reset(token)
    assert refused == {
        "error": "refused_on_device",
        "reason": "high_needs_local_confirmation",
        "device": refused["device"],
    }
    assert len(nodes.pi.sandbox.runs) == 1


def test_without_a_node_in_the_task_there_is_no_pi(tmp_path: Path) -> None:
    nodes = WorldNodes(tmp_path / "receipts")
    nodes.show([PI_NAME])
    nodes.show([])
    assert nodes.entries() == [] and nodes.devices == frozenset()

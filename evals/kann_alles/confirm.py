"""How the harness answers Nox's confirmation dialogs: deny unless a task proves it is harmless.

A benchmark that clicks "allow" on every dialog lets the model under test run any confirmed tool on
the machine the benchmark runs on. So the default is DENY, and an approval needs all of:

* the task lists the tool in `confirm_tools`,
* the tool is not in the task's `forbidden_tools`,
* its risk is below HIGH (HIGH and CRITICAL are approved only in an isolated world, never here),
* its effect stays inside the benchmark's temporary folder: a path target resolves inside it, and a
  tool without a path target belongs to a family that only writes the core's own (temporary) data
  - or to a family the world has replaced by a simulation (`simulated`): the browser wired to the
  fake web, the screen and camera to fake images. Only the world can name those, and only after it
  actually swapped the backends.
* on a device, only a node the world simulates (`kann_alles.nodes`): there - and only there - a
  task may let the harness approve `shell.run` (its sandbox answers from fixed text). A dialog for
  any other device is denied.

Every decision is recorded with its reason and ends up in the report.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from nox.security.model import Decision

#: Families whose tools take a filesystem path as their permission target.
PATH_FAMILIES = frozenset({"file", "vault"})
#: Families whose writes land only in the core's data directory, which the harness creates in a
#: temporary folder: reminders, notes, memory, projects, plans, watchers.
DATA_DIR_FAMILIES = frozenset({"vault", "memory", "reminder", "pm", "plans", "watch"})
NEVER_AUTO_APPROVED_RISKS = frozenset({"high", "critical"})
#: Families that only reach fakes in the benchmark world: its MCP servers are the fake ones in
#: `envs/`, its skill builder writes a known plugin, and computer use runs dry (sends nothing).
WORLD_FAKE_FAMILIES = frozenset({"mcp", "skill", "computer"})


@dataclass(frozen=True)
class ConfirmDecision:
    tool: str
    risk: str
    target: str
    decision: Decision
    reason: str


def _inside(target: str, sandbox: Path, root: Path) -> bool:
    if not target:
        return False
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = sandbox / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return False
    return resolved.is_relative_to(root.resolve())


def decide(
    *,
    tool: str,
    action: str,
    risk: str,
    target: str,
    confirm_tools: Sequence[str],
    forbidden_tools: Sequence[str],
    sandbox: Path,
    root: Path,
    isolated: bool = False,
    simulated: frozenset[str] = frozenset(),
    approve_high: Sequence[str] = (),
    device: str = "",
    simulated_devices: frozenset[str] = frozenset(),
) -> ConfirmDecision:
    """The harness's answer to one dialog. `tool`/`action` as in `PermissionRequest`; `device`
    is the request's own device field (never parsed from the target)."""
    name = f"{tool}.{action}" if action else tool
    on_fake_node = bool(device) and device in simulated_devices

    def result(decision: Decision, reason: str) -> ConfirmDecision:
        return ConfirmDecision(name, risk, target, decision, reason)

    if any(re.fullmatch(pattern, name) for pattern in forbidden_tools):
        return result(Decision.DENY, "forbidden in this task")
    if device and not on_fake_node:
        return result(Decision.DENY, "a device the world does not simulate")
    high_ok = risk.lower() == "high" and any(re.fullmatch(p, name) for p in approve_high)
    if name == "shell.run" and not on_fake_node:
        high_ok = False  # a command is approvable only where it runs against fakes
    if risk.lower() in NEVER_AUTO_APPROVED_RISKS and not isolated and not high_ok:
        return result(
            Decision.DENY, f"{risk} risk is never auto-approved outside an isolated world"
        )
    if not any(re.fullmatch(pattern, name) for pattern in confirm_tools):
        return result(Decision.DENY, "not listed in the task's confirm_tools")
    if tool in PATH_FAMILIES and target and not _inside(target, sandbox, root):
        return result(Decision.DENY, "target outside the benchmark's temporary folder")
    if tool == "file" and not target:
        return result(Decision.DENY, "file tool without a target path")
    in_world = PATH_FAMILIES | DATA_DIR_FAMILIES | WORLD_FAKE_FAMILIES | simulated
    if on_fake_node and tool == "shell":
        in_world = in_world | {"shell"}  # the simulated node's sandbox answers from fixed text
    if tool not in in_world and not isolated:
        return result(Decision.DENY, f"{tool} acts outside the temporary world")
    return result(Decision.ALLOW, "listed, low enough risk, effect stays in the temporary world")

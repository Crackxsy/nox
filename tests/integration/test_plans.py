"""A plan end to end: proposed by the model, approved by the user, carried out by the queue.

The unit tests use a fake executor, which is right for testing the restart semantics and exactly
the shape that cannot notice a plan whose every step comes back `permission.denied`. That happened
to the preset runner for a week of green unit tests, so this file runs the real thing: a real
`NoxCore`, the real permission engine, the real task queue, and steps that really execute.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

from nox.app import DEFAULTS_PATH, NoxCore
from nox.core.config import load_config
from nox.data.repos import TaskStatus
from nox.ipc.dispatch import RequestContext
from nox.ipc.protocol import Envelope, Kind, Source
from nox.security.model import Decision, PermissionRequest, Risk
from tests._ports import free_port_base


def _config(tmp_path: Path) -> Any:
    base = free_port_base()
    (tmp_path / "vault").mkdir()
    overrides = {
        "paths": {
            "data_dir": str(tmp_path / "data"),
            "vault_dir": str(tmp_path / "vault"),
            "index_dir": str(tmp_path / "index"),
            "database_dir": str(tmp_path / "db"),
            "cache_dir": str(tmp_path / "cache"),
            "backups_dir": str(tmp_path / "backups"),
            "runtime_dir": str(tmp_path / "runtime"),
            "logs_dir": str(tmp_path / "logs"),
        },
        "ipc": {"port": base, "http_port": base + 1},
        "privacy": {"mode": "balanced"},
        "security": {"profile": "companion"},
        "health": {"check_interval_s": 3600},
        "logging": {"level": "WARNING"},
        "plugins": {"enabled": []},
    }
    return load_config(DEFAULTS_PATH, None, None, overrides)


@pytest.fixture
async def core(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    user_config = tmp_path / "user.yaml"
    user_config.write_text(yaml.safe_dump({}), encoding="utf-8")
    monkeypatch.setenv("NOX_USER_CONFIG", str(user_config))
    monkeypatch.setenv("NOX_CONFIG_DEFAULTS", str(DEFAULTS_PATH))

    instance = NoxCore(_config(tmp_path), voice=False)
    await asyncio.wait_for(instance.start(), timeout=60)
    try:
        yield instance
    finally:
        await asyncio.wait_for(instance.stop(), timeout=30)


def _ctx(role: str = "dashboard") -> RequestContext:
    return RequestContext(
        client_id=f"{role}:1",
        role=role,
        request=Envelope(kind=Kind.REQUEST, name="plans.list", src=Source(role=role, id="1")),
    )


async def _request(core: NoxCore, request: str, /, **arguments: Any) -> dict[str, Any]:
    registration = core.registry.get(request)
    assert registration is not None, f"{request} is not registered"
    return await registration.handler(_ctx(), registration.payload_model(**arguments))


async def _as_model(core: NoxCore, tool: str, /, **arguments: Any) -> Any:
    """Call a tool the way the model does - through the executor and its permission check."""
    return await core.tool_executor.call(
        agent="companion", name=tool, arguments=arguments, mode="companion"
    )


async def _finished(core: NoxCore, task_id: str, timeout: float = 20.0) -> Any:
    """Wait for the queue to finish one task. The queue polls, so this polls too."""
    from nox.plans.install import PlansRuntime

    runtime = core.extensions["plans"]
    assert isinstance(runtime, PlansRuntime)
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        row = runtime.book.tasks.get(task_id)
        if row is not None and row.status in (TaskStatus.DONE, TaskStatus.FAILED):
            return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"task {task_id} did not finish within {timeout}s")


def steps(*tools: str) -> list[dict[str, Any]]:
    return [{"tool": tool, "why": "im Test"} for tool in tools]


# ---- the wiring ------------------------------------------------------------------------------


async def test_the_extension_is_installed(core: NoxCore) -> None:
    assert core.extensions.get("plans") is not None


async def test_the_model_can_see_the_plan_tools(core: NoxCore) -> None:
    for expected in ("plans.propose", "plans.waiting", "plans.start", "plans.status"):
        assert expected in core.tool_registry.names(), expected


async def test_the_model_may_propose_but_not_start_on_its_own(core: NoxCore) -> None:
    """The safety property of the whole feature, checked against the real permission engine.

    Asked rather than executed on purpose. Executing it is what *should* happen: the engine opens a
    confirmation and waits up to a minute for the user, which in a test with nobody watching is a
    minute of nothing. The decision is the assertion; the dialog is the proof it works.
    """
    proposed = await _as_model(core, "plans.propose", title="Zeit", steps=steps("time.now"))
    assert proposed.ok, proposed.error
    assert proposed.decision == Decision.ALLOW.value, "proposing writes nothing and must be free"

    decided = core.security.engine.preview(
        PermissionRequest(
            agent="companion", tool="plans", action="start", mode="companion", risk=Risk.HIGH
        )
    )

    assert decided.decision is Decision.CONFIRM, "the user has to be asked, every profile"


# ---- a plan that really runs --------------------------------------------------------------------


async def test_an_approved_plan_runs_every_step(core: NoxCore) -> None:
    proposed = await _as_model(
        core, "plans.propose", title="Zweimal Zeit", steps=steps("time.now", "time.now")
    )
    plan_id = proposed.data["plan"]["id"]

    approved = await _request(core, "plans.approve", plan=plan_id)
    row = await _finished(core, approved["task"])

    assert row.status is TaskStatus.DONE, row.error
    results = (row.checkpoint or {}).get("done", [])
    assert [entry["ok"] for entry in results] == [True, True]
    assert (row.checkpoint or {}).get("attempting") is None


async def test_the_status_tool_reports_what_happened(core: NoxCore) -> None:
    proposed = await _as_model(core, "plans.propose", title="Zeit", steps=steps("time.now"))
    plan_id = proposed.data["plan"]["id"]
    approved = await _request(core, "plans.approve", plan=plan_id)
    await _finished(core, approved["task"])

    status = await _as_model(core, "plans.status", plan=plan_id)

    assert status.data["status"] == "done"
    assert status.data["steps_done"] == status.data["steps_total"] == 1


async def test_a_step_that_does_not_work_fails_the_plan_and_names_the_step(core: NoxCore) -> None:
    """A plan that stopped has to say where. "The plan failed" on its own helps nobody.

    The second step asks `capabilities.check` for an empty name, which its input model rejects - a
    step that fails for a reason no profile can change, so the assertion holds in every build.
    """
    proposed = await _as_model(
        core,
        "plans.propose",
        title="Eins gut, eins kaputt",
        steps=[{"tool": "time.now"}, {"tool": "capabilities.check", "arguments": {"name": ""}}],
    )
    approved = await _request(core, "plans.approve", plan=proposed.data["plan"]["id"])

    row = await _finished(core, approved["task"])

    assert row.status is TaskStatus.FAILED
    assert "step 1" in row.error and "capabilities.check" in row.error
    results = (row.checkpoint or {}).get("done", [])
    assert results[0]["ok"] is True, "the step before the broken one still ran"
    assert results[1]["ok"] is False


async def test_a_discarded_plan_cannot_be_started(core: NoxCore) -> None:
    proposed = await _as_model(core, "plans.propose", title="Weg", steps=steps("time.now"))
    plan_id = proposed.data["plan"]["id"]

    await _request(core, "plans.discard", plan=plan_id)
    listing = await _request(core, "plans.list")

    assert [plan["id"] for plan in listing["waiting"]] == []

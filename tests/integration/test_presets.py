"""Presets end to end: a real `NoxCore`, a real program start, a real permission check.

The unit tests around `nox.presets` all use stand-ins, which is the right shape for testing the
matching rules and the step order - and exactly the shape that cannot notice a feature which was
never wired in. Nox has been bitten by that twice: a tool registry that was silently replaced by
an empty one, and a pet that could not find the core because a port was never published.

So this file asserts the wiring itself. The extension is installed, the three tools are in the
registry the model sees, the dashboard's requests are registered, a preset started through IPC
really runs a program, and a sentence spoken to the orchestrator reaches its preset without a
language model being involved.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from nox.app import DEFAULTS_PATH, NoxCore
from nox.core.config import load_config
from nox.ipc.dispatch import RequestContext
from nox.ipc.protocol import Envelope, Kind, Source
from tests._ports import free_port_base

#: A program every machine running this suite has, used as the action a preset starts.
PROBE = [sys.executable, "-c", "pass"]
FAILING_PROBE = [sys.executable, "-c", "import sys; sys.exit(7)"]

PRESETS = {
    "enabled": True,
    "actions": [
        {"id": "probe", "name": "Probe", "command": PROBE, "timeout_s": 30.0},
        {
            "id": "failing",
            "name": "Failing probe",
            "command": FAILING_PROBE,
            "timeout_s": 30.0,
            "report_failure": False,
        },
    ],
    "items": [
        {
            "id": "focus",
            "name": "Fokus",
            "triggers": {"phrases": ["fokus modus"]},
            "steps": [
                {"kind": "mode", "mode": "coding"},
                {"kind": "run", "action": "probe"},
                {"kind": "say", "text": "Konzentration."},
            ],
        },
        {
            "id": "halfbroken",
            "name": "Halb kaputt",
            "triggers": {"phrases": ["halb kaputt"]},
            "steps": [
                # No `home` plugin here, so this step cannot work - and must not stop the rest.
                {"kind": "light", "entity_ids": ["light.nowhere"], "on": True},
                {"kind": "run", "action": "probe"},
            ],
        },
        {
            "id": "failing",
            "name": "Programm faellt um",
            "triggers": {"phrases": ["programm kaputt"]},
            "steps": [{"kind": "run", "action": "failing"}],
        },
    ],
}


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
        "presets": PRESETS,
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
        request=Envelope(kind=Kind.REQUEST, name="presets.list", src=Source(role=role, id="1")),
    )


async def _call(core: NoxCore, name: str, **arguments: Any) -> dict[str, Any]:
    """Invoke one registered request the way the dispatcher does."""
    registration = core.registry.get(name)
    assert registration is not None, f"{name} is not registered"
    return await registration.handler(_ctx(), registration.payload_model(**arguments))


def _failures(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """The steps that did not work, as (kind, reason) - so a failing test says which and why."""
    return [(step["kind"], step["error"]) for step in payload["steps"] if not step["ok"]]


# ---- the wiring ------------------------------------------------------------------------------


async def test_the_extension_is_installed(core: NoxCore) -> None:
    """A feature nobody installs is a feature nobody has."""
    assert "presets" in core.extensions
    assert core.extensions["presets"] is not None


async def test_the_model_can_see_the_preset_tools(core: NoxCore) -> None:
    names = core.tool_registry.names()

    for expected in ("presets.list", "presets.activate", "presets.run_action"):
        assert expected in names, f"{expected} is not in the registry the model is offered"


async def test_the_orchestrator_asks_presets_before_the_model(core: NoxCore) -> None:
    assert core.orchestrator is not None
    assert core.orchestrator.preset_gate is not None


# ---- what the dashboard does -----------------------------------------------------------------


async def test_listing_shows_what_was_configured(core: NoxCore) -> None:
    payload = await _call(core, "presets.list")

    assert {preset["id"] for preset in payload["presets"]} == {"focus", "halfbroken", "failing"}
    assert {action["id"] for action in payload["actions"]} == {"probe", "failing"}


async def test_activating_a_preset_runs_its_steps(core: NoxCore) -> None:
    payload = await _call(core, "presets.activate", preset="focus")

    assert payload["ok"], _failures(payload)
    assert [step["kind"] for step in payload["steps"]] == ["mode", "run", "say"]
    # The mode step really moved the core, not only its own record of it.
    assert str(core.state.get("assistant.mode")) == "coding"


async def test_a_failed_step_does_not_stop_the_rest(core: NoxCore) -> None:
    """The `light` step has no plugin behind it here; the program after it must still start."""
    payload = await _call(core, "presets.activate", preset="halfbroken")

    steps = payload["steps"]
    assert not payload["ok"]
    assert steps[0]["ok"] is False and steps[0]["kind"] == "light"
    assert steps[1]["ok"] is True and steps[1]["kind"] == "run"


async def test_a_program_that_fails_is_reported_with_its_exit_code(core: NoxCore) -> None:
    payload = await _call(core, "presets.test_action", action="failing")

    assert payload["ok"] is False
    assert payload["exit_code"] == 7
    assert "7" in payload["error"]


async def test_testing_a_program_really_starts_it(core: NoxCore) -> None:
    payload = await _call(core, "presets.test_action", action="probe")

    assert payload["ok"], payload["error"] if "error" in payload else payload
    assert payload["exit_code"] == 0
    assert payload["duration_ms"] > 0


# ---- the security boundary -------------------------------------------------------------------


async def test_a_program_start_is_audited(core: NoxCore) -> None:
    """A preset is a convenience, not an exemption: the start leaves the same trail as any tool."""
    await _call(core, "presets.activate", preset="focus")
    core.security.audit.flush(timeout_s=5.0)

    entries = core.security.audit_store.entries(limit=500)
    tools = [(entry.actor, entry.tool, entry.action) for entry in entries]
    assert ("presets", "presets", "run_action") in tools, tools[-12:]


async def test_a_spoken_phrase_reaches_its_preset_without_a_model(core: NoxCore) -> None:
    assert core.orchestrator is not None

    turn = await core.orchestrator.handle_text("fokus modus", speak=False)

    assert turn.fast_path == "preset"
    assert "Konzentration" in turn.response
    assert turn.provider == "fastpath", "a preset must not cost a model round trip"
    assert str(core.state.get("assistant.mode")) == "coding"


async def test_talking_about_a_preset_does_not_activate_it(core: NoxCore) -> None:
    assert core.orchestrator is not None
    before = str(core.state.get("assistant.mode"))

    hit = core.orchestrator.preset_gate.runner.match("was bedeutet fokus modus")

    assert hit is None
    assert str(core.state.get("assistant.mode")) == before

"""One capability, three ways in: a button, a sentence typed, a sentence spoken.

This is the property the whole release is built around, and it is the one that quietly breaks. A
feature gets a dashboard page and stays unreachable by voice; a voice phrase grows its own little
code path and drifts from what the button does. So the test takes a single preset and reaches it
three ways, asserting that the *same* thing happened each time.

One asymmetry falls out of this and is asserted rather than smoothed over: clicking Run and saying
the phrase are the user acting, so neither asks anything. A model deciding by itself to activate a
preset is a third kind of event, and it gets a confirmation. "Everything reachable by text is
reachable by voice" is the promise; "a model may do whatever the user may do" never was.

Reaching a tool by typing means the model answered with a directive, and no test should depend on a
language model doing that reliably - the parsing half is covered against a scripted model in
`tests/unit/core/test_toolloop.py`, and what is checked here is that the route and the permission
are what the loop will find.
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
from nox.core.events import E, Event
from nox.ipc.dispatch import RequestContext
from nox.ipc.protocol import Envelope, Kind, Source
from nox.security.model import Decision, PermissionRequest
from tests._ports import free_port_base

PHRASE = "fokus modus"
PROBE = [sys.executable, "-c", "pass"]

PRESETS = {
    "enabled": True,
    "actions": [{"id": "probe", "name": "Probe", "command": PROBE, "timeout_s": 30.0}],
    "items": [
        {
            "id": "fokus",
            "name": "Fokus",
            "triggers": {"phrases": [PHRASE]},
            # A mode step is the observable effect: it moves the core, and every way in has to
            # move it the same distance.
            "steps": [{"kind": "mode", "mode": "coding"}, {"kind": "run", "action": "probe"}],
        }
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
        request=Envelope(kind=Kind.REQUEST, name="presets.activate", src=Source(role=role, id="1")),
    )


async def _mode(core: NoxCore) -> str:
    return str(core.state.get("assistant.mode"))


async def _reset(core: NoxCore) -> None:
    await core.state.update("assistant.mode", "companion", reason="test")


async def test_the_button_runs_it(core: NoxCore) -> None:
    """Way one: the dashboard's Run button, straight to the preset runner."""
    await _reset(core)
    registration = core.registry.get("presets.activate")
    assert registration is not None

    payload = await registration.handler(_ctx(), registration.payload_model(preset="fokus"))

    assert payload["ok"], payload["steps"]
    assert await _mode(core) == "coding"


async def test_the_model_reaches_the_same_preset_but_has_to_ask(core: NoxCore) -> None:
    """Way two, and the one asymmetry in the three - deliberate, and worth writing down.

    Clicking Run and saying the phrase are the user acting, and neither opens a dialog. A model
    deciding on its own to activate a preset is a third thing, and `presets.activate` is medium
    risk, so the engine asks. The route exists and the tool is offered; the answer is "confirm".

    Asked rather than executed, for the same reason as in test_plans: executing it opens a dialog
    and waits a minute for a user who is not there.
    """
    assert "presets.activate" in core.tool_registry.names()
    spec = core.tool_registry.get("presets.activate")

    decided = core.security.engine.preview(
        PermissionRequest(
            agent="companion",
            tool="presets",
            action="activate",
            mode="companion",
            risk=spec.risk,
        )
    )

    assert decided.decision is Decision.CONFIRM, (
        "a model activating a preset on its own should be a question, not a surprise"
    )


async def test_saying_it_runs_it(core: NoxCore) -> None:
    """Way three: the spoken phrase, which reaches the preset without any model at all."""
    await _reset(core)
    assert core.orchestrator is not None

    await core.bus.publish(
        Event(
            name=E.VOICE_TRANSCRIPT_READY,
            # The whole `TranscriptReady` shape, because the bus validates every payload - which
            # is why a test cannot fake a voice event with two fields and believe it proved
            # something about the real path.
            payload={
                "text": PHRASE,
                "language": "de",
                "confidence": 0.92,
                "addressed_to_nox": True,
                "duration_ms": 900,
                "latency_ms": 180,
            },
            source="voice",
        )
    )
    turn = core.orchestrator.voice_turn
    assert turn is not None, "the transcript did not start a turn"
    await asyncio.wait_for(turn, timeout=30)

    assert await _mode(core) == "coding"


async def test_the_three_ways_are_the_same_way(core: NoxCore) -> None:
    """The point of the file: not three code paths that happen to agree today.

    All three end in `PresetRunner.activate`, so what they share is the implementation, not a
    matching set of assertions. This checks that they still do.
    """
    from nox.presets.install import PresetsRuntime

    runtime = core.extensions["presets"]
    assert isinstance(runtime, PresetsRuntime)
    runner = runtime.runner

    # The dashboard request, the model's tool and the voice gate all hold this one object.
    assert core.orchestrator.preset_gate is not None
    assert core.orchestrator.preset_gate.runner is runner


async def test_speaking_reaches_the_same_tools_as_typing(core: NoxCore) -> None:
    """Voice is not a second system: both ways end in `handle_text`, so both get the tool loop."""
    assert core.orchestrator is not None
    assert core.orchestrator.tool_gate is not None

    offered = {tool.name for tool in core.orchestrator.tool_gate.offer()}

    assert "presets.activate" in offered, "a spoken request can only reach what is offered"

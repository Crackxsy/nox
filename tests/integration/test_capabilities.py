"""The capability catalogue against a real core: real profiles, real permission engine.

The unit tests use a stand-in engine, which is right for testing what the report *says* and exactly
the shape that cannot notice a catalogue nobody can reach. Nox has been bitten by that: every
preset step came back `permission.denied` for a week of unit-test green, because no profile had a
rule for the agent the steps ran under.

So this file asks the awkward question about the catalogue itself - can the model actually call it,
in every profile the user can switch to? A self-description that is refused in the offline profile
is worse than none, because that is precisely when the user needs to hear what is still possible.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

from nox.ai.tooluse import SENTINEL
from nox.app import DEFAULTS_PATH, NoxCore
from nox.core.config import load_config
from nox.ipc.dispatch import RequestContext
from nox.ipc.protocol import Envelope, Kind, Source
from nox.security.model import Decision, PermissionRequest
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
        # Nothing enabled: the plugins are all present on disk, so this is the state the catalogue
        # has to describe as "installed, switched off" rather than as absent.
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
        request=Envelope(
            kind=Kind.REQUEST, name="capabilities.report", src=Source(role=role, id="1")
        ),
    )


async def _request(core: NoxCore, request: str, /, **arguments: Any) -> dict[str, Any]:
    """Invoke one registered IPC request the way the dispatcher does."""
    registration = core.registry.get(request)
    assert registration is not None, f"{request} is not registered"
    return await registration.handler(_ctx(), registration.payload_model(**arguments))


async def _as_model(core: NoxCore, tool: str, /, **arguments: Any) -> Any:
    """Call a tool the way the model does - through the executor, permission check and all.

    Positional-only on purpose: `capabilities.check` takes an argument called `name`, and a keyword
    parameter of that name here would collide with it.
    """
    return await core.tool_executor.call(
        agent="companion", name=tool, arguments=arguments, mode="companion"
    )


# ---- the wiring ------------------------------------------------------------------------------


async def test_the_extension_is_installed(core: NoxCore) -> None:
    assert "capabilities" in core.extensions


async def test_the_model_can_see_the_capability_tools(core: NoxCore) -> None:
    names = core.tool_registry.names()

    for expected in ("capabilities.list", "capabilities.check"):
        assert expected in names, f"{expected} is not in the registry the model is offered"


async def test_the_model_really_gets_an_answer(core: NoxCore) -> None:
    """Registered is not the same as callable - the permission check sits in between."""
    result = await _as_model(core, "capabilities.list")

    assert result.ok, result.error
    assert result.decision == Decision.ALLOW.value
    assert result.data is not None and result.data["usable"]


async def test_every_profile_lets_nox_describe_itself(core: NoxCore) -> None:
    """A self-description refused in the offline profile is worse than none.

    That is exactly the moment the user needs to hear what is still possible, and it is the moment
    a profile written as a whitelist would quietly take it away.
    """
    available = core.security.profiles.available()
    assert len(available) >= 5, available
    refused: list[str] = []

    for profile_id in available:
        core.security.engine.set_profile(profile_id, by="test")
        result = await _as_model(core, "capabilities.list")
        if not result.ok:
            refused.append(f"{profile_id}: {result.error}")
    core.security.engine.set_profile("companion", by="test")

    assert refused == []


async def test_the_dashboard_gets_the_full_report(core: NoxCore) -> None:
    payload = await _request(core, "capabilities.report")

    assert payload["profile"] == "companion"
    assert payload["privacy_mode"] == "balanced"
    # The dashboard draws a page, so it needs the descriptions the tool summary leaves out.
    assert all(entry["description"] for entry in payload["capabilities"])


# ---- what it says ----------------------------------------------------------------------------


async def test_the_catalogue_lists_itself(core: NoxCore) -> None:
    """If Nox cannot see its own catalogue, nothing it says about itself can be checked."""
    result = await _as_model(core, "capabilities.list")

    assert "capabilities.check" in result.data["usable"]


async def test_the_report_follows_the_profile_it_was_taken_under(core: NoxCore) -> None:
    core.security.engine.set_profile("offline", by="test")
    try:
        payload = await _request(core, "capabilities.report")
    finally:
        core.security.engine.set_profile("companion", by="test")

    assert payload["profile"] == "offline"


async def test_a_plugin_that_is_switched_off_is_reported_as_such(core: NoxCore) -> None:
    """Nine plugins sit on disk and none is enabled. "Absent" would be the wrong word for that."""
    payload = await _request(core, "capabilities.report")
    disabled = {gap["name"] for gap in payload["gaps"] if gap["reason"] == "disabled"}

    assert "home.*" in disabled, sorted(disabled)


async def test_what_nox_cannot_do_reaches_the_model(core: NoxCore) -> None:
    result = await _as_model(core, "capabilities.list")
    missing = {gap["name"]: gap["why"] for gap in result.data["missing"]}

    assert missing["desktop.software_install"] == "missing"
    # The two boundaries, and they must not read like backlog items.
    assert missing["game.input"] == "forbidden"
    assert missing["home.lock"] == "forbidden"


async def test_checking_one_thing_names_the_rule_that_decided(core: NoxCore) -> None:
    result = await _as_model(core, "capabilities.check", name="capabilities.list")

    assert result.data["known"] and result.data["possible"]
    assert result.data["rule"], "an answer nobody can trace to a rule cannot be argued with"


# ---- tools in a conversation -------------------------------------------------------------------


async def test_the_orchestrator_got_a_tool_gate(core: NoxCore) -> None:
    """Everything reachable from the dashboard has to be reachable by saying it."""
    assert core.orchestrator is not None
    assert core.orchestrator.tool_gate is not None


async def test_the_offer_is_what_the_model_may_actually_call(core: NoxCore) -> None:
    """The presets bug in one sentence: offered under one agent, carried out as another.

    So this walks the whole offer and asks the real permission engine about each entry under the
    agent the gate will use. Anything the model is told about must survive that check.
    """
    gate = core.orchestrator.tool_gate
    offered = gate.offer()
    assert offered, "a companion profile with nothing usable would be a broken installation"

    refused = []
    for tool in offered:
        name, _, action = tool.name.partition(".")
        spec = core.tool_registry.get(tool.name)
        assert spec is not None, f"{tool.name} is offered but not registered"
        decided = core.security.engine.preview(
            PermissionRequest(
                agent="companion", tool=name, action=action, mode="companion", risk=spec.risk
            )
        )
        if decided.decision is Decision.DENY:
            refused.append(f"{tool.name} ({decided.rule_id})")

    assert refused == []


async def test_a_tool_that_asks_first_is_offered_and_marked(core: NoxCore) -> None:
    """Hiding it would take the decision away from the person the dialog is for."""
    gate = core.orchestrator.tool_gate
    marked = [tool.name for tool in gate.offer() if tool.asks_first]

    assert marked, "some tool in the companion profile must need a confirmation"


async def test_the_prompt_section_names_the_protocol_once(core: NoxCore) -> None:
    section = core.orchestrator.tool_gate.prompt()

    assert section.count(SENTINEL) == 1
    assert "capabilities.check" in section


async def test_the_offer_stays_within_its_budget(core: NoxCore) -> None:
    """The section rides along on every tool-enabled turn, so its size is a latency decision.

    Two bounds, because they catch different mistakes. The total is a budget: 6000 characters
    is about 1500 tokens, affordable for a cloud model and already noticeable for the small
    local one Nox falls back to. The average per tool is the more useful canary - it fails when
    a single tool arrives with a paragraph of description, which the total would hide for a
    while.

    Measured as features landed: 26 tools / 3668 characters, then 30 / 4110, 43 / 5591, now 44 /
    5888 - roughly 1400 tokens a turn. An earlier version of this docstring said the answer at
    this point was a relevance filter. Designing one changed that: the tool names and descriptions
    are English and the user writes German, so lexical relevance misses `home.light` for "mach das
    Licht an" and a filter that misses *hides a capability*, which is a worse failure than a long
    prompt. Doing it properly means German and English trigger words supplied per tool at
    registration - a deliberate design step, not something to squeeze in behind a number.

    So the budget is 8000 and the reason is written down. The per-tool average below is what still
    catches the mistake this test is really for: one tool arriving with a paragraph about itself.
    """
    section = core.orchestrator.tool_gate.prompt()
    offered = len(core.orchestrator.tool_gate.offer())
    print(f"\ntool offer: {offered} tools, {len(section)} characters")

    assert len(section) < 8000, f"{len(section)} characters is over the per-turn budget"
    assert len(section) / offered < 160, "some tool is describing itself at length"

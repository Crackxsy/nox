"""The node extension hangs onto the core through named, ordered extension points - so a second
extension can join the same ones without taking the first one's place, a doubled installation is
refused where it happens, and a core that lacks a part the hub needs is named at once."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from nox.core.hooks import DuplicateHookError
from nox.data.db import Database
from nox.node.hub.install import install
from nox.node.hub.ports import CoreServices, NodeWiringError
from nox.node.hub.repo import NodeRepository
from nox.node.wire import protocol as p
from tests.unit.node.support import pair_node
from tests.unit.node.support_voice import lan_accepts
from tests.unit.node.support_wiring import (
    BASE_FACTS,
    Recorder,
    build_core,
    capability_probe,
    fire_connected,
    installed,
    name_of_satellite,
    recording_session,
)


@pytest.fixture
def db(tmp_path: Path) -> Any:
    database = Database(tmp_path / "nox.db")
    database.migrate()
    try:
        yield database
    finally:
        database.close()


async def _online_node(runtime: Any, tmp_path: Path, name: str, tools: dict[str, str]) -> str:
    node = await pair_node(runtime.pairing, tmp_path, name=name)
    entry = runtime.registry._entry(node.node_id, name)
    entry.online, entry.tools = True, tools
    return str(node.node_id)


# ---- a second extension joins the same extension points ---------------------------------


async def test_another_extensions_fact_line_sits_after_the_device_line_not_in_its_place(
    tmp_path: Path, db: Database
) -> None:
    async with installed(tmp_path, db) as (core, runtime):
        await _online_node(runtime, tmp_path, "Laptop", {})
        core.orchestrator.add_fact_source("weather", lambda: "Rain later.")
        line = runtime.registry.prompt_line()
        assert core.orchestrator._facts() == f"{BASE_FACTS}\n{line}\nRain later."


async def test_a_fact_line_registered_before_the_node_extension_comes_before_its_line(
    tmp_path: Path, db: Database
) -> None:
    core = build_core(tmp_path, db)
    core.orchestrator.add_fact_source("weather", lambda: "Rain later.")
    runtime = install(core)
    try:
        await _online_node(runtime, tmp_path, "Laptop", {})
        line = runtime.registry.prompt_line()
        assert core.orchestrator._facts() == f"{BASE_FACTS}\nRain later.\n{line}"
    finally:
        await runtime.stop()
        core.security.close()


async def test_another_probe_answers_for_tools_the_nodes_say_nothing_about(
    tmp_path: Path, db: Database
) -> None:
    from nox.capabilities.model import CapabilityState

    async with installed(tmp_path, db) as (core, _runtime):
        off = (CapabilityState.UNAVAILABLE, "switched off")
        core.tool_registry.add_availability_probe(
            "other", lambda tool: off if tool == "x.y" else None
        )
        assert capability_probe(core, "x.y") == off
        assert capability_probe(core, "notify.show")[1] == "no paired device with this is online"


async def test_another_label_provider_names_what_the_nodes_do_not_know(
    tmp_path: Path, db: Database
) -> None:
    stranger = "e" * 32
    async with installed(tmp_path, db) as (core, runtime):
        node_id = await _online_node(runtime, tmp_path, "Living room Pi", {})
        core.security.engine.add_satellite_label(
            "other", lambda device: "Porch Pi" if device == stranger else ""
        )
        assert name_of_satellite(core, stranger) == "Porch Pi"
        assert name_of_satellite(core, node_id) == f"Living room Pi · {node_id[:4]}"


async def test_another_overlay_check_can_only_take_a_credential_away(
    tmp_path: Path, db: Database
) -> None:
    async with installed(tmp_path, db) as (core, runtime):
        node_id = await _online_node(runtime, tmp_path, "Stream PC", {"obs.request": "low"})
        NodeRepository(db).set_reported_groups(node_id, {"obs": True}, when=datetime.now(UTC))
        credential = await runtime.satellites.handle(
            recording_session(node_id, Recorder()),
            p.M_OVERLAY_CREDENTIAL,
            p.OverlayCredentialRequest(),
        )
        assert credential is not None and lan_accepts(core.tokens, credential["token"])
        allowed = {"value": True}
        core.tokens.add_node_overlay_check("curfew", lambda _device: allowed["value"])
        assert lan_accepts(core.tokens, credential["token"])  # the node's own check still says yes
        allowed["value"] = False
        assert not lan_accepts(core.tokens, credential["token"])


async def test_another_extensions_connection_hook_runs_after_the_nodes_own(
    tmp_path: Path, db: Database
) -> None:
    async with installed(tmp_path, db) as (core, runtime):
        recorder = Recorder()
        node_id = await _online_node(runtime, tmp_path, "Laptop", {})
        runtime.registry.on_connected = recorder.spy("registry.on_connected")  # type: ignore[method-assign]
        runtime.satellites.push_gates = recorder.spy("satellites.push_gates")  # type: ignore[method-assign]
        runtime.listener.on_connected.append(recorder.spy("another extension"))
        await fire_connected(runtime.listener, recording_session(node_id, recorder))
        assert recorder.calls == [
            "registry.on_connected",
            "request nox/games",
            "satellites.push_gates",
            "another extension",
        ]


async def test_another_stop_hook_does_not_replace_the_nodes_stop(
    tmp_path: Path, db: Database
) -> None:
    async with installed(tmp_path, db) as (core, runtime):
        recorder = Recorder()
        runtime.listener.broadcast_kill = recorder.spy("nodes stop")  # type: ignore[method-assign]
        core.security.killswitch.register_stop_hook("other", recorder.spy("other stop"))
        await core.security.killswitch.engage("user", "test")
        assert sorted(recorder.calls) == ["nodes stop", "other stop"]


# ---- installing twice, or in another order ------------------------------------------------


async def test_installing_twice_is_refused_and_leaves_the_first_installation_alone(
    tmp_path: Path, db: Database
) -> None:
    async with installed(tmp_path, db) as (core, runtime):
        await _online_node(runtime, tmp_path, "Laptop", {})
        before = core.orchestrator._facts()
        tools = core.tool_registry.names()
        requests = sorted(core.registry._handlers)
        with pytest.raises(NodeWiringError, match="already installed"):
            install(core)
        assert core.orchestrator._facts() == before  # the device line is there once
        assert core.tool_registry.names() == tools
        assert sorted(core.registry._handlers) == requests


async def test_installing_twice_is_refused_while_network_access_is_off_as_well(
    tmp_path: Path, db: Database
) -> None:
    async with installed(tmp_path, db, network=False) as (core, _runtime):
        with pytest.raises(NodeWiringError, match="already installed"):
            install(core)


async def test_a_pairing_route_that_is_already_served_is_never_taken_over(
    tmp_path: Path, db: Database
) -> None:
    core = build_core(tmp_path, db)

    async def other_route(_body: Any) -> tuple[int, dict[str, Any]]:
        return 200, {}

    core.lan_runtime.node_pair = other_route
    try:
        with pytest.raises(NodeWiringError, match="already installed"):
            install(core)
        assert core.lan_runtime.node_pair is other_route
        assert core.registry.get("node.list") is None  # nothing was wired before the refusal
    finally:
        core.security.close()


async def test_an_overlay_check_decides_the_same_whoever_installed_first(
    tmp_path: Path, db: Database
) -> None:
    """The extension points that mean "every one must agree" give the same answer in any order:
    a permissive second check changes nothing, a vetoing one always wins."""
    answers: dict[tuple[bool, bool], bool] = {}
    for other_first in (True, False):
        for verdict in (True, False):
            core = build_core(tmp_path / f"{other_first}{verdict}", db)
            if other_first:
                core.tokens.add_node_overlay_check("other", lambda _device, v=verdict: v)
            runtime = install(core)
            try:
                if not other_first:
                    core.tokens.add_node_overlay_check("other", lambda _device, v=verdict: v)
                name = f"Stream PC {len(answers)}"  # a name is paired once per database
                node_id = await _online_node(runtime, tmp_path, name, {"obs.request": "low"})
                groups = {"obs": True}
                NodeRepository(db).set_reported_groups(node_id, groups, when=datetime.now(UTC))
                credential = await runtime.satellites.handle(
                    recording_session(node_id, Recorder()),
                    p.M_OVERLAY_CREDENTIAL,
                    p.OverlayCredentialRequest(),
                )
                assert credential is not None
                answers[(other_first, verdict)] = lan_accepts(core.tokens, credential["token"])
            finally:
                await runtime.stop()
                core.security.close()
    assert answers == {
        (True, True): True,
        (True, False): False,
        (False, True): True,
        (False, False): False,
    }


async def test_a_name_another_extension_took_is_refused_loudly_not_overwritten(
    tmp_path: Path, db: Database
) -> None:
    core = build_core(tmp_path, db)
    core.orchestrator.add_fact_source("nodes", lambda: "Somebody else's line.")
    with pytest.raises(DuplicateHookError, match="fact source 'nodes'"):
        install(core)
    assert core.orchestrator._facts() == f"{BASE_FACTS}\nSomebody else's line."
    core.security.close()


# ---- a core that lacks a part ---------------------------------------------------------------


async def test_a_core_without_a_part_is_named_before_anything_is_wired(
    tmp_path: Path, db: Database
) -> None:
    core = build_core(tmp_path, db)
    core.tokens = None
    del core.hub
    try:
        with pytest.raises(NodeWiringError) as refused:
            install(core)
        assert "core.tokens" in str(refused.value) and "core.hub" in str(refused.value)
        assert "core.registry" not in str(refused.value)  # only what is missing is named
        assert core.registry.get("node.list") is None
        assert core.tool_registry.names() == []
    finally:
        core.security.close()


async def test_every_part_the_hub_reads_is_required(tmp_path: Path, db: Database) -> None:
    from nox.node.hub.ports import REQUIRED_PARTS

    for part in REQUIRED_PARTS:
        core = build_core(tmp_path / part, db)
        security = core.security
        setattr(core, part, None)
        try:
            with pytest.raises(NodeWiringError, match=f"core.{part}"):
                CoreServices.of(core)
        finally:
            security.close()


async def test_the_network_extension_is_the_one_part_that_may_be_missing(
    tmp_path: Path, db: Database
) -> None:
    core = build_core(tmp_path, db)
    del core.lan_runtime  # the `lan` extension is not installed
    try:
        runtime = install(core)
        assert not runtime.enabled and runtime.listener is None
        assert CoreServices.of(core).lan is None
    finally:
        core.security.close()

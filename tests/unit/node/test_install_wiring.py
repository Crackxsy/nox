"""`install(core)` of the node extension, run against a core built from its real parts: what it
registers, what it hooks onto the core, and in which order what happens. These are the properties
the rest of Nox relies on without knowing it - the turn facts name the paired devices after the
core's own facts, a node that connects is listed before its gates are pushed, a removed node
loses its overlay credential - so each has a test that goes red when the wiring changes them."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from nox.capabilities.model import CapabilityState
from nox.core.events.names import E
from nox.data.db import Database
from nox.node.hub.repo import NodeRepository
from nox.node.wire import protocol as p
from nox.node.wire.protocol import NodePairRequest
from nox.security.model import Risk
from nox.tools.registry import ToolSpec
from tests.unit.node.support import pair_node
from tests.unit.node.support_voice import lan_accepts
from tests.unit.node.support_wiring import (
    BASE_FACTS,
    Recorder,
    WiredCore,
    capability_probe,
    fire_connected,
    fire_disconnected,
    installed,
    name_of_satellite,
    recording_session,
)

REQUESTS = frozenset(
    {
        "node.computer.release",
        "node.computer.status",
        "node.defaults.set",
        "node.link",
        "node.list",
        "node.narrow",
        "node.pair.start",
        "node.unpair",
    }
)

#: The tools a paired PC can be asked to run; `obs.request` is the plugin's tool, routed.
ROUTED_TOOLS = frozenset(
    {
        "browser.click",
        "browser.open",
        "browser.read",
        "browser.submit",
        "browser.type",
        "camera.snapshot",
        "computer.keys",
        "computer.pointer",
        "computer.session_end",
        "computer.session_start",
        "desktop.process_stop",
        "desktop.processes",
        "desktop.type_text",
        "desktop.window_close",
        "desktop.window_focus",
        "desktop.window_minimize",
        "desktop.window_restore",
        "desktop.windows",
        "file.delete",
        "file.list",
        "file.move",
        "file.read",
        "file.write",
        "media.control",
        "notify.show",
        "obs.request",
        "screen.capture",
        "shell.run",
        "system.info",
        "voice.say",
    }
)


@pytest.fixture
def db(tmp_path: Path) -> Any:
    database = Database(tmp_path / "nox.db")
    database.migrate()
    try:
        yield database
    finally:
        database.close()


async def _paired_online(
    core: WiredCore, runtime: Any, tmp_path: Path, name: str, tools: dict[str, str]
) -> tuple[str, Any]:
    """A paired node that is online with `tools`; returns its id and registry entry."""
    node = await pair_node(runtime.pairing, tmp_path, name=name)
    entry = runtime.registry._entry(node.node_id, name)
    entry.online, entry.tools = True, tools
    return node.node_id, entry


# ---- what is registered ------------------------------------------------------------------------


async def test_install_registers_the_node_requests_and_routes_the_tools(
    tmp_path: Path, db: Database
) -> None:
    async with installed(tmp_path, db) as (core, runtime):
        assert runtime.enabled and runtime.listener is not None and runtime.registry is not None
        assert runtime.satellites is not None
        assert frozenset(core.registry._handlers) == REQUESTS
        assert frozenset(core.tool_registry.names()) == ROUTED_TOOLS


async def test_without_network_access_only_the_requests_exist(tmp_path: Path, db: Database) -> None:
    """The dashboard says honestly why nodes are unavailable: the requests answer, nothing else is
    wired - no routes, no facts, no listener."""
    async with installed(tmp_path, db, network=False) as (core, runtime):
        assert not runtime.enabled and runtime.listener is None and runtime.satellites is None
        assert frozenset(core.registry._handlers) >= {"node.list", "node.pair.start"}
        assert core.tool_registry.names() == []
        assert core.orchestrator._facts() == BASE_FACTS
        assert capability_probe(core, "notify.show") is None
        assert core.lan_runtime.node_pair is None
        assert core.registry.policy.condition_names() == ()


# ---- the turn facts ----------------------------------------------------------------------------


async def test_the_turn_facts_name_the_devices_after_the_cores_own_facts(
    tmp_path: Path, db: Database
) -> None:
    async with installed(tmp_path, db) as (core, runtime):
        assert core.orchestrator._facts() == BASE_FACTS  # no node paired: nothing to add
        await _paired_online(core, runtime, tmp_path, "Laptop", {})
        line = runtime.registry.prompt_line()
        assert line.startswith("Geräte: ") and "Laptop" in line
        assert core.orchestrator._facts() == f"{BASE_FACTS}\n{line}"


async def test_the_device_line_stands_alone_when_the_core_has_no_facts(
    tmp_path: Path, db: Database
) -> None:
    from tests.unit.node.support_wiring import build_core

    core = build_core(tmp_path, db)
    core.orchestrator.facts = None
    from nox.node.hub.install import install

    runtime = install(core)
    try:
        assert core.orchestrator._facts() == ""
        await _paired_online(core, runtime, tmp_path, "Laptop", {})
        assert core.orchestrator._facts() == runtime.registry.prompt_line() != ""
    finally:
        await runtime.stop()
        core.security.close()


async def test_a_failing_device_line_costs_the_whole_facts_not_the_turn(
    tmp_path: Path, db: Database
) -> None:
    async with installed(tmp_path, db) as (core, runtime):
        await _paired_online(core, runtime, tmp_path, "Laptop", {})

        def broken() -> str:
            raise RuntimeError("no line today")

        runtime.registry.prompt_line = broken  # type: ignore[method-assign]
        assert core.orchestrator._facts() == ""


# ---- the capability report ---------------------------------------------------------------------


async def test_a_tool_only_nodes_have_is_unavailable_while_none_with_it_is_online(
    tmp_path: Path, db: Database
) -> None:
    async with installed(tmp_path, db) as (core, runtime):
        gone = (CapabilityState.UNAVAILABLE, "no paired device with this is online")
        assert capability_probe(core, "notify.show") == gone
        assert capability_probe(core, "voice.say") == gone
        assert capability_probe(core, "not.a.routed.tool") is None
        await _paired_online(core, runtime, tmp_path, "Laptop", {"notify.show": "low"})
        assert capability_probe(core, "notify.show") is None
        assert capability_probe(core, "voice.say") == gone  # this node does not offer it


async def test_a_tool_the_core_has_itself_is_never_reported_missing(
    tmp_path: Path, db: Database
) -> None:
    from nox.node.device.hands.args import PathArgs
    from nox.node.hub.install import install
    from tests.unit.node.support_wiring import build_core

    async def list_folder(_payload: dict[str, Any]) -> dict[str, Any]:
        return {}

    core = build_core(tmp_path, db)
    core.tool_registry.register(
        ToolSpec(
            name="file.list",
            description="The core's own.",
            input_model=PathArgs,
            risk=Risk.READ,
            handler=list_folder,
            side_effects=False,
        )
    )
    runtime = install(core)
    try:
        assert capability_probe(core, "file.list") is None  # it runs here, no node needed
        assert capability_probe(core, "file.read") is not None  # the core has no such tool
    finally:
        await runtime.stop()
        core.security.close()


# ---- how a satellite is named in a question ----------------------------------------------------


async def test_a_satellite_is_named_by_its_node_in_a_question(tmp_path: Path, db: Database) -> None:
    async with installed(tmp_path, db) as (core, runtime):
        node_id, entry = await _paired_online(core, runtime, tmp_path, "Living room Pi", {})
        assert name_of_satellite(core, node_id) == entry.label == f"Living room Pi · {node_id[:4]}"
        unknown = "f" * 32
        assert name_of_satellite(core, unknown) == unknown[:8]


# ---- a connecting node: what happens, and in which order ---------------------------------------


async def test_a_connecting_node_is_listed_and_given_the_games_before_its_gates_are_pushed(
    tmp_path: Path, db: Database
) -> None:
    async with installed(tmp_path, db) as (core, runtime):
        recorder = Recorder()
        node_id, _entry = await _paired_online(core, runtime, tmp_path, "Laptop", {})
        recorder.replace(runtime.registry, "on_connected", "registry.on_connected")
        recorder.replace(runtime.satellites, "push_gates", "satellites.push_gates")
        recorder.replace(runtime.satellites, "panic", "satellites.panic")
        session = recording_session(node_id, recorder)
        await fire_connected(runtime.listener, session)
        assert recorder.calls == [
            "registry.on_connected",
            "request nox/games",
            "satellites.push_gates",
        ]
        assert session.client.requests == [("nox/games", {"add": ["RocketLeague.exe"]})]


async def test_a_node_connecting_during_a_panic_gets_the_panic_after_its_gates(
    tmp_path: Path, db: Database
) -> None:
    async with installed(tmp_path, db) as (core, runtime):
        recorder = Recorder()
        node_id, _entry = await _paired_online(core, runtime, tmp_path, "Stream PC", {})
        recorder.replace(runtime.registry, "on_connected", "registry.on_connected")
        recorder.replace(runtime.satellites, "push_gates", "satellites.push_gates")
        recorder.replace(runtime.satellites, "panic", "satellites.panic")
        await core.security.privacy.set_panic(True)
        recorder.calls.clear()  # the panic itself reached the satellites that were online
        await fire_connected(runtime.listener, recording_session(node_id, recorder))
        assert recorder.calls == [
            "registry.on_connected",
            "request nox/games",
            "satellites.push_gates",
            "satellites.panic",
        ]


async def test_a_node_whose_hello_claims_a_session_is_dropped_before_it_is_listed(
    tmp_path: Path, db: Database
) -> None:
    from nox.node.hub.world_routes import SessionReport

    async with installed(tmp_path, db) as (core, runtime):
        recorder = Recorder()
        node_id, _entry = await _paired_online(core, runtime, tmp_path, "Laptop", {})
        recorder.replace(runtime.registry, "on_connected", "registry.on_connected")
        recorder.replace(runtime.satellites, "push_gates", "satellites.push_gates")
        recorder.replace(runtime.listener, "drop", "listener.drop")
        session = recording_session(node_id, recorder)
        session.computer = SessionReport(gen=4, active=True, id="")
        await fire_connected(runtime.listener, session)
        # Not listed, no games - and the satellites' own hook still runs: it only sends the gate.
        assert recorder.calls == ["listener.drop", "satellites.push_gates"]


async def test_a_node_that_goes_is_forgotten_by_the_satellites_before_it_is_marked_offline(
    tmp_path: Path, db: Database
) -> None:
    async with installed(tmp_path, db) as (core, runtime):
        recorder = Recorder()
        node_id, entry = await _paired_online(core, runtime, tmp_path, "Laptop", {})
        session = recording_session(node_id, recorder)
        entry.session = session
        online_when_satellites_hear: list[bool] = []
        forget = runtime.satellites.disconnected

        def spy(device_id: str) -> None:
            online_when_satellites_hear.append(entry.online)
            forget(device_id)

        runtime.satellites.disconnected = spy  # type: ignore[method-assign]
        await fire_disconnected(runtime.listener, session)
        assert online_when_satellites_hear == [True]
        assert entry.online is False and entry.session is None


# ---- events ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event", [E.PRIVACY_CAPTURE_CHANGED, E.SECURITY_KILL_SWITCH, E.SYSTEM_STARTED]
)
async def test_these_events_push_the_gates_to_the_satellites(
    tmp_path: Path, db: Database, event: str
) -> None:
    async with installed(tmp_path, db) as (core, runtime):
        recorder = Recorder()
        recorder.replace(runtime.satellites, "push_gates", "push_gates")
        recorder.replace(runtime.satellites, "panic", "panic")
        await core.bus.fire(event)
        assert recorder.calls == ["push_gates"]


async def test_the_panic_event_pushes_the_gates_and_then_the_panic(
    tmp_path: Path, db: Database
) -> None:
    async with installed(tmp_path, db) as (core, runtime):
        recorder = Recorder()
        recorder.replace(runtime.satellites, "push_gates", "push_gates")
        recorder.replace(runtime.satellites, "panic", "panic")
        await core.bus.fire(E.SECURITY_PANIC)
        assert recorder.calls == ["push_gates", "panic"]


# ---- the kill switch ---------------------------------------------------------------------------


async def test_the_kill_switch_stops_and_resumes_the_nodes(tmp_path: Path, db: Database) -> None:
    async with installed(tmp_path, db) as (core, runtime):
        recorder = Recorder()
        sent: list[str] = []

        async def kill(reason: str) -> dict[str, bool]:
            recorder.calls.append("broadcast_kill")
            sent.append(reason)
            return {}

        runtime.listener.broadcast_kill = kill  # type: ignore[method-assign]
        recorder.replace(runtime.listener, "broadcast_resume", "broadcast_resume")
        await core.security.killswitch.engage("user", "test")
        assert recorder.calls == ["broadcast_kill"] and sent == ["kill switch"]
        await core.security.killswitch.resume(pin_ok=True)
        assert recorder.calls == ["broadcast_kill", "broadcast_resume"]


# ---- removing a node ---------------------------------------------------------------------------


async def test_removing_a_node_forgets_its_grants_and_its_overlay_credential(
    tmp_path: Path, db: Database
) -> None:
    async with installed(tmp_path, db) as (core, runtime):
        node_id, _entry = await _paired_online(
            core, runtime, tmp_path, "Stream PC", {"obs.request": "low"}
        )
        NodeRepository(db).set_reported_groups(node_id, {"obs": True}, when=datetime.now(UTC))
        revoked: list[tuple[str, str]] = []
        engine = core.security.engine
        original = engine.revoke_device_grants

        def spy(device_id: str, *, reason: str, by: str = "system") -> int:
            revoked.append((device_id, reason))
            return original(device_id, reason=reason, by=by)

        engine.revoke_device_grants = spy  # type: ignore[method-assign]
        session = recording_session(node_id, Recorder())
        credential = await runtime.satellites.handle(
            session, p.M_OVERLAY_CREDENTIAL, p.OverlayCredentialRequest()
        )
        assert credential is not None and credential["ok"]
        assert lan_accepts(core.tokens, credential["token"])  # the network listener asks the hub
        assert await runtime.pairing.revoke(node_id, reason="dashboard")
        assert revoked == [(node_id, "node removed: dashboard")]
        assert not lan_accepts(core.tokens, credential["token"])
        assert core.disconnected_credentials  # its open overlay connections ended


async def test_narrowing_obs_takes_the_overlay_credential_away(
    tmp_path: Path, db: Database
) -> None:
    async with installed(tmp_path, db) as (core, runtime):
        node_id, _entry = await _paired_online(
            core, runtime, tmp_path, "Stream PC", {"obs.request": "low"}
        )
        NodeRepository(db).set_reported_groups(node_id, {"obs": True}, when=datetime.now(UTC))
        credential = await runtime.satellites.handle(
            recording_session(node_id, Recorder()),
            p.M_OVERLAY_CREDENTIAL,
            p.OverlayCredentialRequest(),
        )
        assert credential is not None and lan_accepts(core.tokens, credential["token"])
        for hook in runtime.registry.on_narrowed:  # what the Devices page does
            await hook(node_id, "obs", True)
        assert not lan_accepts(core.tokens, credential["token"])


# ---- pairing over HTTP, the lock, the routes ---------------------------------------------------


async def test_the_pairing_route_answers_starting_until_the_listener_runs(
    tmp_path: Path, db: Database
) -> None:
    async with installed(tmp_path, db) as (core, _runtime):
        redeem = core.lan_runtime.node_pair
        body = NodePairRequest.model_construct()  # not looked at: the listener is not running
        assert await redeem(body) == (503, {"error": "starting"})


async def test_the_computer_use_lock_is_one_of_the_access_conditions(
    tmp_path: Path, db: Database
) -> None:
    async with installed(tmp_path, db) as (core, runtime):
        assert core.registry.policy.condition_names() == ("node_session",)
        assert runtime.registry.may_delegate("a" * 32) is True  # no session runs


async def test_while_nox_controls_a_node_its_lock_reaches_the_registry_and_the_satellites(
    tmp_path: Path, db: Database
) -> None:
    async with installed(tmp_path, db) as (core, runtime):
        laptop = await _paired_online(core, runtime, tmp_path, "Laptop", {})
        other = await _paired_online(core, runtime, tmp_path, "Desk PC", {})
        assert not runtime.satellites.session_active() and not runtime.satellites.controlled(
            laptop[0]
        )
        runtime.control.answered(laptop[0], {"gen": 2, "active": True, "id": "ab" * 16})
        assert runtime.registry.may_delegate(laptop[0]) is True  # the controlled PC's own dialog
        assert runtime.registry.may_delegate(other[0]) is False  # no other node's dialog
        assert runtime.satellites.session_active() is True
        assert runtime.satellites.controlled(laptop[0]) is True
        assert runtime.satellites.controlled(other[0]) is False


async def test_removing_the_controlled_node_ends_its_lock(tmp_path: Path, db: Database) -> None:
    async with installed(tmp_path, db) as (core, runtime):
        node_id, _entry = await _paired_online(core, runtime, tmp_path, "Laptop", {})
        runtime.control.answered(node_id, {"gen": 2, "active": True, "id": "ab" * 16})
        assert runtime.control.locked(node_id)
        assert await runtime.pairing.revoke(node_id, reason="dashboard")
        assert not runtime.control.locked(node_id) and not runtime.control.active()


async def test_the_listener_serves_the_satellites_and_the_runtime_keeps_its_three_tasks(
    tmp_path: Path, db: Database
) -> None:
    async with installed(tmp_path, db) as (core, runtime):
        assert runtime.listener.on_satellite == runtime.satellites.handle
        assert {task.get_name() for task in runtime.tasks} == {
            "nox-node-overlay-sweep",
            "nox-node-computer",
            "nox-node-listener",
        }


# ---- fail closed: what the wiring decides when it cannot tell ---------------------------------


class _PrivacyThatCannotSay:
    """A privacy service whose panic flag cannot be read."""

    @property
    def panic(self) -> bool:
        raise RuntimeError("cannot tell")

    def allows_capture(self, _kind: str) -> bool:
        return True


class _PrivacyThatFails:
    """A privacy service that cannot say whether a microphone may listen."""

    panic = False

    def allows_capture(self, _kind: str) -> bool:
        raise RuntimeError("cannot tell")


class _PrivacyThatAllows:
    panic = False

    def allows_capture(self, _kind: str) -> bool:
        return True


async def _voice_node(core: WiredCore, runtime: Any, tmp_path: Path, privacy: Any) -> Any:
    """A connected voice node (the fake session leaves with `runtime.listener.sessions.clear()`),
    with `privacy` in place of the core's."""
    name = "Living room Pi"
    node_id, _entry = await _paired_online(core, runtime, tmp_path, name, {"voice.say": "low"})
    session = recording_session(node_id, Recorder())
    runtime.listener.sessions[node_id] = session
    core.security.privacy = privacy
    return session


def _last_gate(session: Any) -> dict[str, Any]:
    gates = [params for method, params in session.client.sent if method == p.M_VOICE_GATE]
    assert gates, "the node was never sent a gate"
    return dict(gates[-1])


async def test_a_panic_state_that_cannot_be_read_counts_as_a_panic(
    tmp_path: Path, db: Database
) -> None:
    async with installed(tmp_path, db) as (core, runtime):
        recorder = Recorder()
        node_id, _entry = await _paired_online(core, runtime, tmp_path, "Stream PC", {})
        runtime.registry.on_connected = recorder.spy("registry.on_connected")  # type: ignore[method-assign]
        runtime.satellites.push_gates = recorder.spy("satellites.push_gates")  # type: ignore[method-assign]
        runtime.satellites.panic = recorder.spy("satellites.panic")  # type: ignore[method-assign]
        core.security.privacy = _PrivacyThatCannotSay()
        await fire_connected(runtime.listener, recording_session(node_id, recorder))
        assert recorder.calls[-1] == "satellites.panic"


async def test_a_microphone_that_cannot_be_checked_stays_closed(
    tmp_path: Path, db: Database
) -> None:
    async with installed(tmp_path, db) as (core, runtime):
        session = await _voice_node(core, runtime, tmp_path, _PrivacyThatFails())
        try:
            await runtime.satellites.push_gates()
            assert _last_gate(session) == {"capture": False, "reason": "unknown"}
        finally:
            runtime.listener.sessions.clear()


async def test_an_engaged_kill_switch_closes_the_microphone_of_a_satellite(
    tmp_path: Path, db: Database
) -> None:
    async with installed(tmp_path, db) as (core, runtime):
        session = await _voice_node(core, runtime, tmp_path, _PrivacyThatAllows())
        try:
            await runtime.satellites.push_gates()
            assert _last_gate(session) == {"capture": True, "reason": ""}  # nothing else stops it
            await core.security.killswitch.engage("user", "test")
            await runtime.satellites.push_gates()
            assert _last_gate(session) == {"capture": False, "reason": "stopped"}
        finally:
            runtime.listener.sessions.clear()


async def test_widening_obs_again_is_not_answered_from_the_two_second_cache(
    tmp_path: Path, db: Database
) -> None:
    """The hub's answer to "may this node hold an overlay credential" is cached for a moment; a
    change of the Devices page empties that cache for the node at once."""
    async with installed(tmp_path, db) as (core, runtime):
        node_id, _entry = await _paired_online(
            core, runtime, tmp_path, "Stream PC", {"obs.request": "low"}
        )
        nodes = NodeRepository(db)
        nodes.set_reported_groups(node_id, {"obs": True}, when=datetime.now(UTC))
        credential = await runtime.satellites.handle(
            recording_session(node_id, Recorder()),
            p.M_OVERLAY_CREDENTIAL,
            p.OverlayCredentialRequest(),
        )
        assert credential is not None and lan_accepts(core.tokens, credential["token"])  # cached
        nodes.narrow(node_id, "obs", True, when=datetime.now(UTC))  # the database now says no ...
        for hook in runtime.registry.on_narrowed:  # ... and the Devices page says "cleared"
            await hook(node_id, "obs", False)
        assert not lan_accepts(core.tokens, credential["token"])  # not the answer from before


async def test_the_registry_knows_the_core_id_only_once_the_listener_exists(
    tmp_path: Path, db: Database
) -> None:
    async with installed(tmp_path, db) as (_core, runtime):
        registry, listener = runtime.registry, runtime.listener
        assert registry._core_id() == listener.core_id  # noqa: SLF001
        runtime.listener = None
        assert registry._core_id() == ""  # noqa: SLF001
        runtime.listener = listener

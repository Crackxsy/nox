"""The lock a computer-use session puts on the core, derived from the node's session
reports: only a session this core started holds it, generations order the reports, a stale
or unreadable report never opens it, and a silent node keeps it for the longest session."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nox.node.device.world import world_tools
from nox.node.hub.world_routes import MAX_SESSION_MINUTES, NodeActions, NodeControl, NodeLookups
from nox.node.wire.protocol import NodeHello, Ping, SessionReport
from nox.security.model import PermissionRequest, Risk
from nox.security.permissions import grant_matches
from tests.unit.node.support import registry_entry
from tests.unit.node.support_sessions import (
    BROWSER,
    LAPTOP,
    OTHER_NODE,
    PinGate,
    device_world,
    locked_control,
    privacy_state,
    request_ctx,
    route_call,
    routed_specs,
    session_report,
)


async def test_a_stop_ends_the_session_on_the_node() -> None:
    world, seen = device_world()
    tools = world_tools(world)
    start = tools["computer.session_start"]
    await start.handler(start.args.model_validate({"purpose": "Fenster ordnen"}))
    world.stop("stopped_by_core")  # what `NodeAgent.on_stop` calls on nox/kill
    pointer = tools["computer.pointer"]
    answer = await pointer.handler(pointer.args.model_validate({"action": "click"}))
    assert answer["ok"] is False and world.session_report()["active"] is False
    assert seen["sender"].events == []
    await asyncio.sleep(0)  # the notices are shown in the background
    assert any("mouse and keyboard" in n for n in seen["notices"])


def _answer_with(report: dict[str, Any] | None, **result: Any) -> dict[str, Any]:
    answer: dict[str, Any] = {"device": "Laptop", "result": {"ok": True, **result}}
    if report is not None:
        answer["_session"] = report
    return answer


async def _start(specs: dict[str, Any]) -> dict[str, Any]:
    return await route_call(specs, "computer.session_start", {"purpose": "Datei speichern"})


async def test_a_node_session_grants_input_on_that_node_only_and_shows_who_is_controlled(
    tmp_path: Path,
) -> None:
    async def answer(entry: Any, tool: str, args: Any, **kw: Any) -> dict[str, Any]:
        if tool == "computer.session_start":
            return _answer_with(session_report(2, True), minutes=5)
        return _answer_with(session_report(3, False), done="click")

    specs, _reg, control, (granted, notices, _ends) = routed_specs(
        tmp_path, privacy_state(), answer
    )
    started = await _start(specs)
    assert "_session" not in started  # the node's report is for the core, never the model
    grant = granted[0]
    assert grant.scope == f"device:{LAPTOP}|session:computer/*" and grant.max_risk is Risk.MEDIUM

    def request(device: str) -> PermissionRequest:
        return PermissionRequest(
            agent="companion", tool="computer", action="pointer", mode="companion",
            risk=Risk.MEDIUM, device=device,
        )  # fmt: skip

    from datetime import UTC, datetime

    assert grant_matches(grant, request(LAPTOP), datetime.now(UTC))
    assert not grant_matches(grant, request("b" * 32), datetime.now(UTC))
    assert not grant_matches(grant, request(""), datetime.now(UTC))  # never this core
    assert notices[-1]["active"] and notices[-1]["device"].startswith("Laptop")
    # No paired device may approve while Nox controls a node - none is provably elsewhere;
    # the core machine's own dashboard (no device id) still may.
    assert control.active()  # the lock holds (who may approve: test_node_session_approvals.py)
    # The node reports the session over: notice off, grant revoked, lock lifted.
    await route_call(specs, "computer.pointer", {"action": "click"})
    assert notices[-1]["active"] is False and granted[-1] == grant.grant_id
    assert not control.active()


async def test_a_confirmation_after_a_stop_report_still_locks(tmp_path: Path) -> None:
    """Race 1: a stop's report arrives before the start's answer, which confirms a session."""

    async def answer(entry: Any, tool: str, args: Any, **kw: Any) -> dict[str, Any]:
        control.observe(LAPTOP, session_report(1, False))  # a pong from before the start ran
        return _answer_with(session_report(2, True), minutes=5)

    specs, _reg, control, _ = routed_specs(tmp_path, privacy_state(), answer)
    await _start(specs)
    assert control.locked(LAPTOP) and control.active()


async def test_a_stale_report_never_clears_a_newer_session(tmp_path: Path) -> None:
    """Race 2 and ordering: a late end answer of an older generation changes nothing."""
    late: dict[str, Any] = {}

    async def answer(entry: Any, tool: str, args: Any, **kw: Any) -> dict[str, Any]:
        if tool == "computer.session_start":
            return _answer_with(session_report(5, True), minutes=5)
        return late

    specs, _reg, control, _ = routed_specs(tmp_path, privacy_state(), answer)
    await _start(specs)
    late.update(_answer_with(session_report(3, False), was_open=True))
    await route_call(specs, "computer.session_end", {})
    assert control.active()  # gen 3 < 5: ignored
    control.observe(LAPTOP, {"gen": 4, "active": False})
    control.observe(LAPTOP, "not a report")
    control.observe(LAPTOP, {"gen": 9, "active": "no"})
    assert control.active()  # older or unreadable: nothing changes
    control.observe(LAPTOP, session_report(5, False))  # the same generation, now over (it ran out)
    assert not control.active()


async def test_a_start_without_a_readable_answer_blocks_until_the_node_says_so(
    tmp_path: Path,
) -> None:
    end_report: dict[str, Any] = {"value": None}
    blocked_while_asking: list[bool] = []

    async def answer(entry: Any, tool: str, args: Any, **kw: Any) -> dict[str, Any]:
        if tool == "computer.session_start":
            blocked_while_asking.append(control.active())
            return {"error": "failed_on_device", "detail": "no answer", "device": entry.label}
        return _answer_with(end_report["value"], was_open=False)

    specs, _reg, control, (granted, notices, ends) = routed_specs(tmp_path, privacy_state(), answer)
    await _start(specs)
    assert blocked_while_asking == [True]  # from the moment the start went out
    assert ends == [LAPTOP]  # ended on the node to be sure ...
    assert control.active() and notices[-1]["active"]  # ... no report: still blocked
    assert granted == []  # nothing granted while no session is known
    assert (await _start(specs))["error"] == "session_running"  # no second attempt over it
    end_report["value"] = session_report(7, False)
    await route_call(specs, "computer.session_end", {})
    assert not control.active() and notices[-1]["active"] is False


async def test_a_refusal_with_the_nodes_report_settles_the_start(tmp_path: Path) -> None:
    async def answer(entry: Any, tool: str, args: Any, **kw: Any) -> dict[str, Any]:
        refused = {"error": "refused_on_device", "reason": "game_running", "device": "Laptop"}
        return {**refused, "_session": session_report(4, False)}

    specs, _reg, control, (_granted, _notices, ends) = routed_specs(
        tmp_path, privacy_state(), answer
    )
    await _start(specs)
    assert not control.active() and ends == []


async def test_a_running_session_is_never_replaced_by_a_new_start(tmp_path: Path) -> None:
    calls: list[str] = []

    async def answer(entry: Any, tool: str, args: Any, **kw: Any) -> dict[str, Any]:
        calls.append(tool)
        return _answer_with(session_report(2, True), minutes=5)

    specs, _reg, control, (granted, _notices, _ends) = routed_specs(
        tmp_path, privacy_state(), answer
    )
    await _start(specs)
    assert (await _start(specs))["error"] == "session_running"
    assert calls == ["computer.session_start"]
    assert control.active() and len(granted) == 1  # the grant is untouched


async def test_disconnected_or_silent_nodes_keep_the_lock_for_the_longest_session(
    tmp_path: Path,
) -> None:
    now = {"t": 1000.0}
    control = NodeControl(None, lambda _n: None, clock=lambda: now["t"])
    control.answered(LAPTOP, session_report(2, True))
    now["t"] += 3600  # offline for an hour: no report, still locked
    assert control.active()
    now["t"] += 3 * 3600  # past the longest session since its last word
    assert not control.active()
    # A start the node never answers holds for the longest session as well.
    entry = registry_entry("b" * 32, "Arbeits-PC", linked=BROWSER)

    async def never(**_kw: Any) -> dict[str, Any]:
        raise TimeoutError

    with pytest.raises(TimeoutError):
        await control.around("computer.session_start")(entry, {}, never)
    assert control.active()
    now["t"] += 2 * 3600 + 1
    assert not control.active()


async def test_a_stop_lifts_the_lock_only_through_the_nodes_report() -> None:
    pongs: list[tuple[str, Any]] = []
    notices: list[dict[str, Any]] = []
    control = NodeControl(None, notices.append, NodeLookups(reports=lambda: list(pongs)))
    control.answered(LAPTOP, session_report(2, True))  # the answer to this core's own start
    pongs.append((LAPTOP, session_report(2, True)))
    assert control.active() and notices[-1]["active"]
    pongs.clear()  # the kill switch went out; the node has not answered yet
    assert control.active()
    pongs.append((LAPTOP, session_report(3, False)))  # its next pong: the session ended there
    assert not control.active() and notices[-1]["active"] is False


async def test_a_node_without_a_linked_browser_gets_no_session(tmp_path: Path) -> None:
    calls: list[str] = []

    async def answer(entry: Any, tool: str, args: Any, **kw: Any) -> dict[str, Any]:
        calls.append(tool)
        return _answer_with(session_report(2, True), minutes=5)

    specs, _reg, control, _ = routed_specs(tmp_path, privacy_state(), answer, linked=None)
    refused = await _start(specs)
    assert refused["error"] == "link_required" and "node.link" in refused["hint"]
    assert calls == [] and not control.active()


async def test_the_node_reports_its_sessions_with_a_growing_generation() -> None:
    world, _ = device_world()
    tools = world_tools(world)
    before = world.session_report()
    assert before["active"] is False and before["id"] == ""
    start = tools["computer.session_start"]
    await start.handler(start.args.model_validate({"purpose": "Fenster ordnen"}))
    running = world.session_report()
    assert running["active"] and running["gen"] > before["gen"] and len(running["id"]) == 32
    end = tools["computer.session_end"]
    await end.handler(end.args.model_validate({}))
    over = world.session_report()
    assert over["active"] is False and over["gen"] > running["gen"]


def test_session_reports_are_read_strictly() -> None:
    from nox.node.wire.protocol import SessionReport

    assert Ping.model_validate({"seq": 1, "ts": 1.0, "session": session_report(3, True)}).session
    for bad in (
        {"gen": 1, "active": "no"},
        {"gen": "1", "active": True},
        {"gen": -1, "active": False},
        {"gen": 1, "active": True, "id": "../x"},
    ):
        with pytest.raises(ValueError):
            SessionReport.model_validate(bad)
    # A field of a newer protocol is ignored, the known ones stay strict.
    assert SessionReport.model_validate({"gen": 1, "active": False, "more": 1}).gen == 1
    assert "session" in NodeHello.model_fields


def test_a_same_generation_pong_leaves_a_start_in_flight() -> None:
    control = NodeControl(None, lambda _n: None)
    control.observe(LAPTOP, session_report(5, False))
    device = control._device(LAPTOP, "Laptop")
    from nox.node.hub.world_routes import _Attempt

    device.attempt = _Attempt(control._clock(), device.gen)
    control.observe(LAPTOP, session_report(5, False))  # a pong sent before the start was handled
    assert device.attempt is not None and control.active()
    control.observe(LAPTOP, session_report(6, True))  # a newer generation settles it
    assert device.attempt is None and control.active()


# ---- a cancelled start ---------------------------------------------------------------------------


async def test_a_cancelled_start_ends_the_session_in_the_background(tmp_path: Path) -> None:
    async def answer(entry: Any, tool: str, args: Any, **kw: Any) -> dict[str, Any]:
        if tool == "computer.session_start":
            raise asyncio.CancelledError
        return _answer_with(session_report(9, False), was_open=True)

    specs, _reg, control, (_granted, _notices, ends) = routed_specs(
        tmp_path, privacy_state(), answer
    )
    with pytest.raises(asyncio.CancelledError):
        await _start(specs)
    assert control.active()  # until the node says otherwise
    for _ in range(10):
        await asyncio.sleep(0)
    assert ends == [LAPTOP] and not control.active()


# ---- the smaller points --------------------------------------------------------------------------


def test_a_new_hello_starts_a_new_epoch() -> None:
    control = NodeControl(None, lambda _n: None)
    control.answered(LAPTOP, session_report(2**50, True))
    control.observe(LAPTOP, session_report(7, False))  # a lower generation: ignored ...
    assert control.active()
    control.hello(LAPTOP, session_report(7, False))  # ... unless it is a new run's hello
    assert not control.active()
    control.hello(LAPTOP, None)  # a hello without a report changes nothing
    control.answered(LAPTOP, session_report(8, True))
    control.hello(LAPTOP, "broken")
    assert control.active()


def test_the_epoch_is_random_per_run() -> None:
    first, _ = device_world()
    second, _ = device_world()
    assert first.gen != second.gen and first.gen < 2**62


async def test_grants_say_where_they_come_from(tmp_path: Path) -> None:
    async def answer(entry: Any, tool: str, args: Any, **kw: Any) -> dict[str, Any]:
        return _answer_with(session_report(2, True), minutes=5)

    specs, _reg, _control, (granted, *_rest) = routed_specs(tmp_path, privacy_state(), answer)
    await _start(specs)
    assert granted[0].granted_by == "node-session"


def _clocked() -> tuple[NodeControl, dict[str, float], list[Any]]:
    now = {"t": 0.0}
    granted: list[Any] = []
    engine = SimpleNamespace(grant_temporary=granted.append, revoke=granted.append)
    control = NodeControl(engine, lambda _n: None, clock=lambda: now["t"])
    return control, now, granted


def test_a_long_session_that_keeps_reporting_keeps_the_lock() -> None:
    control, now, _ = _clocked()
    control.answered(LAPTOP, session_report(2, True))
    for _ in range(3 * 60):  # three hours of reports, one a minute
        now["t"] += 60
        control.observe(LAPTOP, session_report(2, True))
        assert control.active()


def test_back_to_back_sessions_keep_the_lock_and_get_a_fresh_grant() -> None:
    control, now, granted = _clocked()
    control.answered(LAPTOP, session_report(2, True, "aa" * 16))
    for gen in range(3, 30, 2):  # ended and started again, every few minutes, for hours
        now["t"] += MAX_SESSION_MINUTES * 6
        control.answered(LAPTOP, session_report(gen, True, f"{gen:02x}" * 16))
        assert control.active()
    grants = [g for g in granted if not isinstance(g, str)]
    assert len({g.grant_id for g in grants}) == len(grants) > 1  # one per session


def test_a_silent_node_lifts_the_lock_after_the_longest_session() -> None:
    control, now, _ = _clocked()
    control.answered(LAPTOP, session_report(2, True))
    now["t"] += MAX_SESSION_MINUTES * 60 - 1
    assert control.active()
    now["t"] += 2
    assert not control.active()


def test_the_owner_releases_a_stuck_lock_and_a_live_session_locks_again() -> None:
    notices: list[dict[str, Any]] = []
    control = NodeControl(None, notices.append)
    control.answered(LAPTOP, session_report(2, True))
    assert control.status() and control.status()[0]["device_id"] == LAPTOP
    assert control.release("released by the owner") == 1
    assert not control.active() and notices[-1]["active"] is False
    assert control.status() == []
    control.observe(LAPTOP, session_report(2, True))  # the node still says it runs
    assert control.active()


async def test_the_release_request_needs_the_pin_here() -> None:
    from nox.ipc.dispatch import RequestRegistry
    from nox.ipc.errors import IpcError
    from nox.node.hub.install.control import ReleaseRequest, register_control_requests
    from tests.unit.node.support_wiring import fake_services

    audit: list[str] = []
    ipc = RequestRegistry()
    control = locked_control()
    core = fake_services(
        registry=ipc,
        security=SimpleNamespace(
            audit=SimpleNamespace(append=lambda **kw: audit.append(kw["decision"]))
        ),
    )
    ipc.policy.pins.gate = lambda: PinGate()
    register_control_requests(core, control)
    handler = ipc._handlers["node.computer.release"].handler
    with pytest.raises(IpcError):
        await handler(request_ctx("dashboard", "dashboard:1"), ReleaseRequest(pin="0000"))
    done = await handler(request_ctx("dashboard", "dashboard:1"), ReleaseRequest(pin="4711"))
    assert done["released"] == 1 and audit == ["deny", "allow"]
    assert ipc._handlers["node.computer.release"].allowed_roles == frozenset({"shell", "dashboard"})


def _violations() -> tuple[NodeControl, list[tuple[str, str]]]:
    seen: list[tuple[str, str]] = []
    control = NodeControl(
        None,
        lambda _n: None,
        NodeLookups(linked=lambda d: BROWSER if d == LAPTOP else None),
        NodeActions(on_violation=lambda device, why: seen.append((device, why))),
    )
    return control, seen


# ---- the lock comes only from a start of this core -----------------------------------------------


def test_a_node_claiming_a_session_in_its_hello_is_a_violation_and_locks_nothing() -> None:
    control, seen = _violations()
    assert control.hello(LAPTOP, session_report(9, True)) is False
    assert seen == [(LAPTOP, "session_in_hello")]
    assert not control.active() and not control.locked(LAPTOP)


def test_a_pong_claiming_a_session_the_core_never_started_locks_nothing() -> None:
    control, seen = _violations()
    assert control.hello(LAPTOP, session_report(1, False))
    control.observe(LAPTOP, session_report(2, True))  # "open" - but this core started nothing
    control.observe(LAPTOP, session_report(3, True, "dd" * 16))
    assert not control.active()
    assert seen == [(LAPTOP, "session_not_started_by_core")]  # once per connection


def test_only_the_session_the_core_started_holds_the_lock() -> None:
    control, seen = _violations()
    control.hello(LAPTOP, session_report(1, False))
    control.answered(
        LAPTOP, session_report(2, True, "aa" * 16)
    )  # the answer to this core's own start
    assert control.locked(LAPTOP)
    control.observe(LAPTOP, session_report(2, True, "aa" * 16))  # its pongs keep it
    assert control.locked(LAPTOP) and seen == []
    control.observe(LAPTOP, session_report(3, False))  # over
    control.observe(LAPTOP, session_report(4, True, "aa" * 16))  # the same id again, unasked
    assert not control.active() and seen == [(LAPTOP, "session_not_started_by_core")]


def test_a_new_connection_forgets_the_started_session() -> None:
    control, seen = _violations()
    control.answered(LAPTOP, session_report(2, True, "aa" * 16))
    assert control.hello(LAPTOP, session_report(1, False))  # a new run: its sessions ended
    control.observe(LAPTOP, session_report(2, True, "aa" * 16))
    assert not control.active() and seen


async def test_the_connection_hook_drops_a_node_whose_hello_claims_a_session() -> None:
    from nox.node.hub.install.control import connection_hook

    control, _seen = _violations()
    dropped: list[tuple[str, str]] = []
    listed: list[str] = []

    class Listener:
        async def drop(self, device_id: str, why: str) -> bool:
            dropped.append((device_id, why))
            return True

    registry = SimpleNamespace(on_connected=lambda s: listed.append(s.device_id))
    hook = connection_hook(SimpleNamespace(), registry, control, Listener())  # type: ignore[arg-type]
    session = SimpleNamespace(device_id=LAPTOP, computer=SessionReport(gen=4, active=True, id=""))
    await hook(session)
    assert dropped == [(LAPTOP, "session_in_hello")] and listed == []


def _world_control(sessions: dict[str, Any], tools: dict[str, dict[str, str]]) -> Any:
    from nox.node.hub.install.control import build_world
    from tests.unit.node.support_wiring import fake_services

    dropped: list[tuple[str, str]] = []

    async def drop(device_id: str, why: str) -> bool:
        dropped.append((device_id, why))
        return True

    listener = SimpleNamespace(sessions=sessions, drop=drop)
    entries = [
        SimpleNamespace(
            device_id=d, session=s, tools=tools.get(d, {}), linked_device_id=BROWSER, label="Laptop"
        )
        for d, s in sessions.items()
    ]
    registry = SimpleNamespace(known=lambda: entries, entries=lambda: entries)
    _specs, control = build_world(fake_services(), registry, dict, listener)  # type: ignore[arg-type]
    return control, registry, dropped


async def test_reports_of_a_node_without_computer_use_are_never_read_as_open() -> None:
    """A session counts as open only from a node offering `computer.session_start` now."""
    session = SimpleNamespace(
        computer=SessionReport(gen=2, active=True, id="ab" * 16),
        peer_host="",
        reported_addresses=[],
    )
    control, _registry_, dropped = _world_control({LAPTOP: session}, {})
    assert not control.active() and dropped == []  # ignored - not even a violation
    session.computer = SessionReport(gen=3, active=False)
    assert not control.active()


async def test_the_core_closes_a_node_that_claims_a_session_it_never_started() -> None:
    session = SimpleNamespace(
        computer=SessionReport(gen=2, active=True, id="ab" * 16),
        peer_host="",
        reported_addresses=[],
    )
    control, registry, dropped = _world_control(
        {LAPTOP: session}, {LAPTOP: {"computer.session_start": "high"}}
    )
    assert not control.active()
    await asyncio.sleep(0)
    assert dropped == [(LAPTOP, "session_not_started_by_core")]
    assert registry.may_delegate(OTHER_NODE)  # wired: no session, every node's dialog may ask


async def test_the_listener_drops_a_node_for_a_violation(
    listener: Any, pairing: Any, tls: Any, tmp_path: Path
) -> None:
    from websockets.exceptions import ConnectionClosed

    from tests.unit.node.support import client_context, greet, open_node, pair_node

    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    await greet(ws)
    for _ in range(50):
        if node.node_id in listener.sessions:
            break
        await asyncio.sleep(0.02)
    assert await listener.drop(node.node_id, "session_not_started_by_core")
    with pytest.raises(ConnectionClosed):
        while True:
            await asyncio.wait_for(ws.recv(), 5)
    assert node.node_id not in listener.sessions

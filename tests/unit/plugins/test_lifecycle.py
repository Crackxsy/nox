"""PluginManager lifecycle, restart backoff, kill switch, health and the core-side plugin IPC
handlers (ST-11-01 acceptance criteria 1, 3-7).

No real process and no real socket: the process factory and the hub are fakes, so the state machine
and the timings are deterministic.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from nox.core.events import E, HealthStatus
from nox.ipc.dispatch import RequestContext, RequestRegistry
from nox.ipc.errors import IpcError
from nox.ipc.protocol import Envelope, Kind, Source
from nox.plugins.manager import (
    PluginManager,
    PluginManagerSettings,
    PluginRegister,
    PluginSecretGet,
    PluginState,
    PluginToolCall,
    PluginToolDeclaration,
)
from nox.security.model import Decision, Risk
from tests.unit.fakes import FakeBus
from tests.unit.plugins.conftest import (
    FakeEngine,
    FakeHub,
    FakeProcess,
    FakeTokens,
    make_profile,
    write_manifest,
)


class FakeSecrets:
    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self.values = dict(values or {})
        self.reads: list[str] = []

    def get(self, name: str) -> str | None:
        self.reads.append(name)
        return self.values.get(name)


class Harness:
    """A PluginManager wired to fakes plus the handles the tests assert on."""

    def __init__(self, manager: PluginManager, hub: FakeHub, bus: FakeBus) -> None:
        self.manager = manager
        self.hub = hub
        self.bus = bus
        self.processes: list[FakeProcess] = []

    def context(self, plugin_id: str = "demo") -> RequestContext:
        client_id = f"plugin:{plugin_id}"
        env = Envelope(
            kind=Kind.REQUEST, name="plugin.register", src=Source(role="plugin", id=client_id)
        )
        return RequestContext(client_id=client_id, role="plugin", request=env)

    async def register(self, plugin_id: str = "demo", tools: list[Any] | None = None) -> Any:
        payload = PluginRegister(
            plugin_id=plugin_id,
            version="0.1.0",
            pid=1234,
            tools=tools
            if tools is not None
            else [PluginToolDeclaration(name=f"{plugin_id}.ping", risk=Risk.READ)],
        )
        return await self.manager._h_register(self.context(plugin_id), payload)


def build(
    plugins_dir: Path,
    *,
    enabled: Sequence[str] = ("demo",),
    engine: FakeEngine | None = None,
    secrets: FakeSecrets | None = None,
    clock: Callable[[], float] | None = None,
    **settings: Any,
) -> Harness:
    bus = FakeBus()
    hub = FakeHub()
    harness_settings = PluginManagerSettings(
        enabled=list(enabled),
        poll_interval_s=0.01,
        backoff_base_s=0.0,
        stop_ack_timeout_s=0.2,
        terminate_timeout_s=0.5,
        **settings,
    )
    manager = PluginManager(
        plugins_dir=plugins_dir,
        bus=bus,
        hub=hub,
        tokens=FakeTokens(),
        registry=RequestRegistry(),
        engine=engine or FakeEngine(),
        secrets=secrets or FakeSecrets(),
        settings=harness_settings,
        **({"clock": clock} if clock is not None else {}),
    )
    harness = Harness(manager, hub, bus)

    def factory(command: Sequence[str], env: Mapping[str, str]) -> FakeProcess:
        assert "--plugin" in command
        assert env["NOX_WORKER_TOKEN"]
        process = FakeProcess(pid=1000 + len(harness.processes))
        harness.processes.append(process)
        return process

    manager._process_factory = factory  # type: ignore[assignment]
    return harness


async def wait_until(predicate: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached in time")


@pytest.fixture
async def harness(plugins_dir: Path):
    write_manifest(plugins_dir, "demo")
    built = build(plugins_dir)
    try:
        yield built
    finally:
        await built.manager.stop("test")


# ---- lifecycle -----------------------------------------------------------------------------------


async def test_full_lifecycle_transitions(harness: Harness) -> None:
    manager = harness.manager
    await manager.start()
    record = manager.records()["demo"]
    assert record.history == [
        PluginState.DISCOVERED,
        PluginState.VALIDATED,
        PluginState.ENABLED,
        PluginState.SPAWNED,
    ]
    response = await harness.register()
    assert response["ok"] is True and response["tools"] == ["demo.ping"]
    assert record.history[-2:] == [PluginState.REGISTERED, PluginState.RUNNING]
    assert harness.hub.services["plugin:demo"] == {"demo"}
    assert manager.tools.names() == ["demo.ping"]
    started = [e for e in harness.bus.published if e.name == E.PLUGIN_STARTED]
    assert started and started[0].payload["plugin_id"] == "demo"

    await manager.stop_all("shutdown")
    assert record.state is PluginState.STOPPED
    assert manager.tools.names() == []


async def test_plugin_not_in_enabled_config_is_not_spawned(plugins_dir: Path) -> None:
    write_manifest(plugins_dir, "demo")
    built = build(plugins_dir, enabled=())
    try:
        await built.manager.start()
        record = built.manager.records()["demo"]
        assert record.state is PluginState.VALIDATED
        assert record.enabled is False and "plugins.enabled" in record.reason
        assert built.processes == []
    finally:
        await built.manager.stop("test")


async def test_profile_mismatch_keeps_the_plugin_unspawned(plugins_dir: Path) -> None:
    write_manifest(plugins_dir, "demo", profiles=["stream"])
    built = build(plugins_dir)  # FakeEngine's active profile is `companion`
    try:
        await built.manager.start()
        record = built.manager.records()["demo"]
        assert record.enabled is False and "profile 'companion'" in record.reason
        assert built.processes == []
    finally:
        await built.manager.stop("test")


async def test_a_misconfigured_plugin_does_not_block_a_good_one(plugins_dir: Path) -> None:
    write_manifest(plugins_dir, "demo")
    write_manifest(
        plugins_dir,
        "broken",
        entry="nox_plugin_broken:create",
        permissions=[{"tool": "obs.switch"}],
    )
    built = build(plugins_dir, enabled=("demo", "broken"))
    try:
        await built.manager.start()
        records = built.manager.records()
        assert records["broken"].state is PluginState.FAILED
        assert "namespace" in records["broken"].reason
        assert records["demo"].state is PluginState.SPAWNED
        assert len(built.processes) == 1
        failed = [e for e in built.bus.published if e.name == E.PLUGIN_FAILED]
        assert [e.payload["plugin_id"] for e in failed] == ["broken"]
    finally:
        await built.manager.stop("test")


async def test_egress_outside_the_profile_fails_the_plugin(plugins_dir: Path) -> None:
    write_manifest(plugins_dir, "demo", network={"egress": ["api.twitch.tv:443"]})
    built = build(plugins_dir)
    try:
        await built.manager.start()
        record = built.manager.records()["demo"]
        assert record.state is PluginState.FAILED
        assert "does not allow" in record.reason
        assert built.processes == []
    finally:
        await built.manager.stop("test")


# ---- restart backoff -----------------------------------------------------------------------------


async def test_crash_restarts_with_backoff_then_fails(plugins_dir: Path) -> None:
    write_manifest(plugins_dir, "demo")
    built = build(plugins_dir, restart_limit=2, restart_window_s=60.0)
    manager = built.manager
    try:
        await manager.start()
        record = manager.records()["demo"]
        for attempt in range(2):
            built.processes[-1].exit(1)
            await wait_until(lambda a=attempt: record.restarts == a + 1)
            await wait_until(lambda a=attempt: len(built.processes) == a + 2)
            assert record.state is PluginState.SPAWNED
        built.processes[-1].exit(1)
        await wait_until(lambda: record.state is PluginState.FAILED)
        assert "exhausted" in record.reason
        assert len(built.processes) == 3  # no fourth spawn
        assert record.restarts == 2
    finally:
        await manager.stop("test")


async def test_restarts_outside_the_window_do_not_count(plugins_dir: Path) -> None:
    write_manifest(plugins_dir, "demo")
    now = [1000.0]  # injected clock: no real sleeps, so a slow CI runner cannot skew the window
    built = build(plugins_dir, restart_limit=1, restart_window_s=60.0, clock=lambda: now[0])
    manager = built.manager
    try:
        await manager.start()
        record = manager.records()["demo"]
        built.processes[-1].exit(1)
        await wait_until(lambda: record.restarts == 1)
        now[0] += 120.0  # the first restart ages out of the window
        built.processes[-1].exit(1)
        await wait_until(lambda: record.restarts == 2)
        assert record.state is PluginState.SPAWNED
    finally:
        await manager.stop("test")


# ---- kill switch ---------------------------------------------------------------------------------


async def test_kill_switch_stops_every_plugin_worker(harness: Harness) -> None:
    manager = harness.manager
    await manager.start()
    await harness.register()
    await manager.stop_all("kill_switch", ack_timeout_s=0.2)
    assert ("plugin:demo", "plugin.stop", {"reason": "kill_switch"}) in harness.hub.requests
    assert harness.processes[0].terminated is True
    record = manager.records()["demo"]
    assert record.state is PluginState.STOPPED
    stopped = [e for e in harness.bus.published if e.name == E.PLUGIN_STOPPED]
    assert stopped and stopped[-1].payload["reason"] == "kill_switch"


async def test_a_worker_that_does_not_ack_is_terminated(harness: Harness) -> None:
    manager = harness.manager
    harness.hub.responses["plugin.stop"] = IpcError("timeout", "no ack")
    await manager.start()
    await harness.register()
    await manager.stop_all("kill_switch", ack_timeout_s=0.05)
    assert harness.processes[0].terminated is True
    assert manager.records()["demo"].state is PluginState.STOPPED


# ---- IPC handlers --------------------------------------------------------------------------------


async def test_secret_get_is_denied_for_undeclared_names(plugins_dir: Path) -> None:
    write_manifest(plugins_dir, "demo", secrets=["nox/demo/token"])
    secrets = FakeSecrets({"nox/demo/token": "s3cret", "nox/other/token": "nope"})
    built = build(plugins_dir, secrets=secrets)
    manager = built.manager
    try:
        await manager.start()
        await built.register()
        ok = await manager._h_secret_get(built.context(), PluginSecretGet(name="nox/demo/token"))
        assert ok == {"name": "nox/demo/token", "value": "s3cret"}
        with pytest.raises(IpcError, match="not declared"):
            await manager._h_secret_get(built.context(), PluginSecretGet(name="nox/other/token"))
        # the undeclared name never reached the secret store
        assert secrets.reads == ["nox/demo/token"]
    finally:
        await manager.stop("test")


async def test_secret_not_set_is_reported_honestly(plugins_dir: Path) -> None:
    write_manifest(plugins_dir, "demo", secrets=["nox/demo/token"])
    built = build(plugins_dir, secrets=FakeSecrets())
    try:
        await built.manager.start()
        await built.register()
        with pytest.raises(IpcError, match="not set in the credential manager"):
            await built.manager._h_secret_get(
                built.context(), PluginSecretGet(name="nox/demo/token")
            )
    finally:
        await built.manager.stop("test")


async def test_register_rejects_tools_outside_the_manifest(harness: Harness) -> None:
    manager = harness.manager
    await manager.start()
    with pytest.raises(IpcError, match="not declared in the manifest"):
        await harness.register(tools=[PluginToolDeclaration(name="demo.sneaky", risk=Risk.READ)])
    assert manager.records()["demo"].state is PluginState.FAILED
    assert manager.tools.names() == []


async def test_register_from_a_foreign_client_id_is_refused(harness: Harness) -> None:
    manager = harness.manager
    await manager.start()
    context = harness.context("demo")
    with pytest.raises(IpcError, match="does not match the connection"):
        await manager._h_register(context, PluginRegister(plugin_id="other", version="1"))
    with pytest.raises(IpcError, match="unknown plugin"):
        await manager._h_register(harness.context("ghost"), PluginRegister(plugin_id="ghost"))


async def test_tool_call_is_permission_checked_and_forwarded(harness: Harness) -> None:
    manager = harness.manager
    harness.hub.responses["tool.call"] = {"text": "pong"}
    await manager.start()
    await harness.register()
    result = await manager._h_tool_call(
        harness.context(), PluginToolCall(name="demo.ping", input={"text": "hi"})
    )
    assert result == {"ok": True, "result": {"text": "pong"}}
    forwarded = [r for r in harness.hub.requests if r[1] == "tool.call"]
    assert forwarded == [
        ("plugin:demo", "tool.call", {"name": "demo.ping", "input": {"text": "hi"}})
    ]


async def test_tool_call_denied_by_the_permission_engine(plugins_dir: Path) -> None:
    write_manifest(plugins_dir, "demo")
    built = build(plugins_dir, engine=FakeEngine(decision=Decision.DENY))
    try:
        await built.manager.start()
        await built.register()
        with pytest.raises(IpcError) as exc:
            await built.manager._h_tool_call(
                built.context(), PluginToolCall(name="demo.ping", input={})
            )
        assert exc.value.code == "permission.denied"
        assert [r for r in built.hub.requests if r[1] == "tool.call"] == []
    finally:
        await built.manager.stop("test")


async def test_tool_call_requiring_confirmation_is_not_faked(plugins_dir: Path) -> None:
    write_manifest(plugins_dir, "demo")
    built = build(plugins_dir, engine=FakeEngine(decision=Decision.CONFIRM))
    try:
        await built.manager.start()
        await built.register()
        with pytest.raises(IpcError, match="not implemented"):
            await built.manager._h_tool_call(
                built.context(), PluginToolCall(name="demo.ping", input={})
            )
    finally:
        await built.manager.stop("test")


async def test_handlers_are_registered_for_the_plugin_role_only(plugins_dir: Path) -> None:
    write_manifest(plugins_dir, "demo")
    built = build(plugins_dir)
    registry = built.manager._registry
    built.manager.register_handlers()
    for name in ("plugin.register", "plugin.secret.get", "plugin.tool.call"):
        registration = registry.get(name)
        assert registration is not None and registration.allowed_roles == frozenset({"plugin"})
        assert registry.is_allowed(name, "plugin") is True
        assert registry.is_allowed(name, "dashboard") is False
        assert registry.is_allowed(name, "worker") is False


# ---- health --------------------------------------------------------------------------------------


async def test_health_is_honest_per_state(plugins_dir: Path) -> None:
    write_manifest(plugins_dir, "demo")
    write_manifest(
        plugins_dir,
        "broken",
        entry="nox_plugin_broken:create",
        permissions=[{"tool": "obs.switch"}],
    )
    built = build(plugins_dir, enabled=("demo", "broken"))
    manager = built.manager
    try:
        await manager.start()
        assert manager.health_of("demo")[0] is HealthStatus.LIMITED  # spawned, not registered
        assert manager.health_of("broken")[0] is HealthStatus.UNAVAILABLE
        assert manager.health_of("nothing_here") == (HealthStatus.UNAVAILABLE, "unknown plugin")
        await built.register()
        status, reason = manager.health_of("demo")
        assert status is HealthStatus.AVAILABLE and "1 tools" in reason
        built.processes[0].exit(0)
        assert manager.health_of("demo") == (HealthStatus.UNAVAILABLE, "worker process gone")
        assert {c.name for c in manager.health_checks()} == {"plugin.demo", "plugin.broken"}
    finally:
        await manager.stop("test")


async def test_status_report_is_serializable(harness: Harness) -> None:
    await harness.manager.start()
    await harness.register()
    status = {s.plugin_id: s for s in harness.manager.status()}
    assert status["demo"].state is PluginState.RUNNING
    assert status["demo"].tools == ["demo.ping"]
    assert status["demo"].model_dump(mode="json")["enabled"] is True


# ---- profile switching (ST-11-01 acceptance criterion 3) -----------------------------------------


async def test_profile_switch_spawns_and_stops_without_a_core_restart(plugins_dir: Path) -> None:
    write_manifest(plugins_dir, "demo", profiles=["stream"])
    engine = FakeEngine()
    built = build(plugins_dir, engine=engine)
    manager = built.manager
    try:
        await manager.start()
        record = manager.records()["demo"]
        assert record.state is PluginState.VALIDATED and built.processes == []

        engine.profile = make_profile("stream")
        await manager.apply_profile()
        assert record.state is PluginState.SPAWNED and len(built.processes) == 1
        await built.register()

        engine.profile = make_profile("companion")
        await manager.apply_profile()
        assert record.state is PluginState.STOPPED
        assert built.processes[0].terminated is True
        assert manager.tools.names() == []
    finally:
        await manager.stop("test")


async def test_nothing_is_spawned_while_the_kill_switch_is_engaged(plugins_dir: Path) -> None:
    write_manifest(plugins_dir, "demo", profiles=["stream"])
    engine = FakeEngine()
    engaged = {"value": True}
    built = build(plugins_dir, engine=engine)
    built.manager._safe_mode = lambda: engaged["value"]  # type: ignore[assignment]
    try:
        await built.manager.start()
        engine.profile = make_profile("stream")
        await built.manager.apply_profile()
        assert built.processes == []
        engaged["value"] = False
        await built.manager.apply_profile()
        assert len(built.processes) == 1
    finally:
        await built.manager.stop("test")

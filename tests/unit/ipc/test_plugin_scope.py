"""The hub holds a plugin connection to its manifest, whatever the plugin process itself does.

These tests speak raw frames, the way plugin code that skipped `PluginApi` would: a subscription to
`**`, a subscription the manifest never listed, an event forged into a reserved namespace, an
event the manifest never declared. Each one must be narrowed or refused by the core.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

from nox.core.events import Event
from nox.ipc.dispatch import RequestRegistry
from nox.ipc.plugin_scope import PLUGIN_BASE_LISTENS, PluginScope, is_reserved_event
from nox.ipc.protocol import NAME_SUBSCRIBE, Envelope, Kind, Source
from nox.ipc.server import MAX_AUDITED_VIOLATIONS, HubSettings, IpcHub
from nox.ipc.tokens import TokenStore
from tests.unit.ipc.conftest import RawClient, SimpleBus

PLUGIN_ID = "plugin:demo"
DEMO_SCOPE = PluginScope.of(listens=["demo.tick", "system.mode_changed"], emits=["demo.pong"])


@pytest.fixture
async def audited_hub(
    bus: SimpleBus, tokens: TokenStore, registry: RequestRegistry, runtime_dir: Path
) -> AsyncIterator[tuple[IpcHub, list[tuple[str, str, str]]]]:
    violations: list[tuple[str, str, str]] = []
    hub = IpcHub(
        HubSettings(port=0),
        tokens,
        registry,
        bus,
        runtime_dir,
        boundary_audit=lambda *row: violations.append(row),
    )
    await hub.start()
    try:
        yield hub, violations
    finally:
        await hub.stop()


PluginFactory = Callable[..., Awaitable[RawClient]]


@pytest.fixture
async def plugin_client(
    audited_hub: tuple[IpcHub, list[tuple[str, str, str]]], tokens: TokenStore
) -> AsyncIterator[PluginFactory]:
    hub, _ = audited_hub
    clients: list[RawClient] = []

    async def open_plugin(scope: PluginScope | None = DEMO_SCOPE) -> RawClient:
        if scope is not None:
            hub.set_plugin_scope(PLUGIN_ID, scope)
        conn = await connect(hub.url, open_timeout=5)
        client = RawClient(conn, Source(role="plugin", id=PLUGIN_ID))
        clients.append(client)
        reply = await client.auth(tokens.issue_worker_token(PLUGIN_ID))
        assert reply.kind is Kind.RESPONSE, reply.payload
        return client

    yield open_plugin
    for client in clients:
        await client.close()


async def _subscribe(client: RawClient, *patterns: str) -> Envelope:
    reply = await client.request(NAME_SUBSCRIBE, {"patterns": list(patterns)})
    assert reply.kind is Kind.RESPONSE, reply.payload
    return reply


async def _received(client: RawClient, timeout: float = 0.3) -> list[str]:
    names: list[str] = []
    while True:
        try:
            env = await client.recv(timeout)
        except TimeoutError:
            return names
        if env.kind is Kind.EVENT:
            names.append(env.name)


async def _publish(bus: SimpleBus, *names: str) -> None:
    for name in names:
        await bus.publish(Event(name=name, payload={}, source="core"))


async def test_a_plugin_subscribing_to_everything_receives_only_its_manifest(
    plugin_client: PluginFactory, bus: SimpleBus
) -> None:
    client = await plugin_client()

    reply = await _subscribe(client, "**")

    assert set(reply.payload["patterns"]) == {*PLUGIN_BASE_LISTENS, *DEMO_SCOPE.listens}
    await _publish(
        bus,
        "sensor.foreground_changed",
        "remote.message",
        "security.permission_requested",
        "home.state_changed",
        "demo.tick",
        "security.kill_switch",
    )
    assert await _received(client) == ["demo.tick", "security.kill_switch"]


async def test_a_subscription_outside_the_manifest_is_denied_and_audited(
    plugin_client: PluginFactory,
    bus: SimpleBus,
    audited_hub: tuple[IpcHub, list[tuple[str, str, str]]],
) -> None:
    _, violations = audited_hub
    client = await plugin_client()

    reply = await _subscribe(client, "remote.message", "demo.tick")

    assert reply.payload["denied"] == ["remote.message"]
    assert "remote.message" not in reply.payload["patterns"]
    assert (PLUGIN_ID, "subscribe", "remote.message") in violations
    await _publish(bus, "remote.message", "demo.tick")
    assert await _received(client) == ["demo.tick"]


async def test_the_worker_lifecycle_patterns_narrow_to_the_events_a_worker_needs(
    plugin_client: PluginFactory, bus: SimpleBus
) -> None:
    client = await plugin_client()

    reply = await _subscribe(client, "security.*", "privacy.*", "system.stopping")

    assert reply.payload["denied"] == []
    await _publish(
        bus,
        "security.kill_switch",
        "security.permission_requested",
        "security.audit",
        "privacy.mode_changed",
        "privacy.zone_changed",
        "system.stopping",
    )
    assert await _received(client) == [
        "security.kill_switch",
        "privacy.mode_changed",
        "system.stopping",
    ]


@pytest.mark.parametrize(
    "forged", ["privacy.capture_changed", "system.started", "security.panic", "voice.kill_phrase"]
)
async def test_a_plugin_can_never_publish_into_a_reserved_namespace(
    plugin_client: PluginFactory,
    bus: SimpleBus,
    audited_hub: tuple[IpcHub, list[tuple[str, str, str]]],
    forged: str,
) -> None:
    """Even a scope that lists the name (a manifest that slipped past validation) is refused."""
    _, violations = audited_hub
    client = await plugin_client(PluginScope.of(listens=[], emits=[forged, "demo.pong"]))

    sent = await client.send(Kind.EVENT, forged, {"microphone": True})
    error = await client.recv()

    assert is_reserved_event(forged)
    assert error.kind is Kind.ERROR and error.corr == sent.id
    assert error.payload["code"] == "permission.denied"
    assert forged not in bus.names()
    assert (PLUGIN_ID, "emit", forged) in violations


async def test_a_plugin_publishes_only_the_exact_names_it_declared(
    plugin_client: PluginFactory, bus: SimpleBus
) -> None:
    client = await plugin_client()

    undeclared = await client.send(Kind.EVENT, "demo.other", {})
    error = await client.recv()
    await client.send(Kind.EVENT, "demo.pong", {"n": 1})
    for _ in range(50):
        if "demo.pong" in bus.names():
            break
        await asyncio.sleep(0.01)

    assert error.kind is Kind.ERROR and error.corr == undeclared.id
    assert "demo.other" not in bus.names()
    published = [e for e in bus.published if e.name == "demo.pong"]
    assert published and published[0].source == PLUGIN_ID


async def test_a_plugin_connection_the_core_never_scoped_gets_lifecycle_events_only(
    plugin_client: PluginFactory, bus: SimpleBus
) -> None:
    client = await plugin_client(scope=None)

    reply = await _subscribe(client, "**", "demo.tick")
    await client.send(Kind.EVENT, "demo.pong", {})
    error = await client.recv()

    assert set(reply.payload["patterns"]) == set(PLUGIN_BASE_LISTENS)
    assert reply.payload["denied"] == ["demo.tick"]
    assert error.kind is Kind.ERROR
    assert "demo.pong" not in bus.names()
    await _publish(bus, "demo.tick", "system.stopping")
    assert await _received(client) == ["system.stopping"]


async def test_a_plugin_hammering_its_boundary_cannot_flood_the_audit_log(
    plugin_client: PluginFactory,
    audited_hub: tuple[IpcHub, list[tuple[str, str, str]]],
) -> None:
    _, violations = audited_hub
    client = await plugin_client()

    for _ in range(MAX_AUDITED_VIOLATIONS + 10):
        await client.send(Kind.EVENT, "system.started", {})
    for _ in range(MAX_AUDITED_VIOLATIONS + 10):
        await client.recv()  # one error frame per refused event

    assert len(violations) == MAX_AUDITED_VIOLATIONS


def test_narrowing_never_widens_a_wildcard_manifest_entry() -> None:
    scope = PluginScope.of(listens=["game.*"], emits=[])

    assert scope.narrow("game.*") == ["game.*"]
    assert scope.narrow("**") == list(PLUGIN_BASE_LISTENS)
    assert scope.narrow("remote.message") == []
    assert scope.may_receive("game.detected") is True
    assert scope.may_receive("remote.message") is False

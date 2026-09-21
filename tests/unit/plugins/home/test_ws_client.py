"""The Home Assistant WebSocket client against a real fake server.

What is protected here: the documented handshake, a token that is rejected staying rejected rather
than looking like a network failure, `id` correlation between a command and its result, event
dispatch, and a reconnect after the server drops the connection.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from .fake_ha_server import VALID_TOKEN, FakeHomeAssistant, wait_until

pytestmark = pytest.mark.timeout(30)


def _client(server: FakeHomeAssistant, token: str | None = VALID_TOKEN, **kwargs: Any) -> Any:
    from nox_plugin_home.ws_client import HomeAssistantClient

    async def provide() -> str | None:
        return token

    events: list[tuple[str, dict[str, Any]]] = []

    async def on_event(name: str, data: dict[str, Any]) -> None:
        events.append((name, data))

    client = HomeAssistantClient(
        server.url,
        token_provider=provide,
        on_event=on_event,
        min_backoff_s=0.05,
        max_backoff_s=0.1,
        request_timeout_s=5.0,
        **kwargs,
    )
    client.received_events = events  # type: ignore[attr-defined]
    return client


async def test_authenticates_with_the_long_lived_token_and_subscribes(
    ha_server: FakeHomeAssistant,
) -> None:
    client = _client(ha_server)
    client.start()
    try:
        await wait_until(lambda: client.authenticated)
        assert client.ha_version == "2026.9.0"
        await wait_until(lambda: ha_server.subscriptions == ["state_changed"])
    finally:
        await client.stop()


async def test_a_wrong_token_never_authenticates_and_says_so(
    ha_server: FakeHomeAssistant,
) -> None:
    client = _client(ha_server, token="wrong")
    client.start()
    try:
        await wait_until(lambda: "rejected the access token" in client.last_error)
        assert client.authenticated is False
    finally:
        await client.stop()


async def test_a_missing_token_reports_the_secret_name_to_set(
    ha_server: FakeHomeAssistant,
) -> None:
    client = _client(ha_server, token=None)
    client.start()
    try:
        await wait_until(lambda: "nox/home/access_token" in client.last_error)
        assert client.authenticated is False
    finally:
        await client.stop()


async def test_commands_are_correlated_by_id(ha_server: FakeHomeAssistant) -> None:
    client = _client(ha_server)
    client.start()
    try:
        await wait_until(lambda: client.authenticated)
        states, registry = await asyncio.gather(client.get_states(), client.registry("area"))
        assert any(row["entity_id"] == "light.wz_decke" for row in states)
        assert {row["name"] for row in registry} == {"Wohnzimmer", "Küche", "Bad"}
    finally:
        await client.stop()


async def test_a_failed_command_raises_with_the_home_assistant_reason(
    ha_server: FakeHomeAssistant,
) -> None:
    from nox_plugin_home.ws_client import HomeCommandError

    ha_server.set_response("get_states", RuntimeError("entity registry locked"))
    client = _client(ha_server)
    client.start()
    try:
        await wait_until(lambda: client.authenticated)
        with pytest.raises(HomeCommandError) as excinfo:
            await client.get_states()
        assert "entity registry locked" in str(excinfo.value)
    finally:
        await client.stop()


async def test_events_reach_the_handler(ha_server: FakeHomeAssistant) -> None:
    client = _client(ha_server)
    client.start()
    try:
        await wait_until(lambda: client.authenticated)
        await ha_server.broadcast_state_changed("light.wz_decke", "off")
        await wait_until(lambda: bool(client.received_events))
        name, data = client.received_events[0]
        assert name == "state_changed"
        assert data["entity_id"] == "light.wz_decke"
    finally:
        await client.stop()


async def test_reconnects_after_the_server_drops_the_connection(
    ha_server: FakeHomeAssistant,
) -> None:
    client = _client(ha_server)
    client.start()
    try:
        await wait_until(lambda: client.authenticated)
        assert ha_server.auth_count == 1
        await ha_server.disconnect_all()
        await wait_until(lambda: ha_server.auth_count >= 2, timeout=10.0)
        await wait_until(lambda: client.authenticated)
    finally:
        await client.stop()


async def test_a_command_without_a_session_fails_instead_of_hanging(
    ha_server: FakeHomeAssistant,
) -> None:
    client = _client(ha_server)
    with pytest.raises(ConnectionError):
        await client.get_states()

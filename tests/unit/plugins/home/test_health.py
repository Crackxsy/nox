"""Honest health and the privacy gate for the `home` plugin.

Two separate claims are protected here. Health never reports AVAILABLE for a connection that does
not exist - not when the token is missing, not when Home Assistant is down, not when the privacy
mode forbids the connection. And the privacy gate is enforced in code: `private` and `offline`
must stop the plugin from dialling even though the profile has to allow-list the loopback endpoint
for the manifest to validate in the first place.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nox.core.events import HealthStatus
from nox.core.state import PrivacyMode
from nox.plugins.api import PrivacyView

from .conftest import FakeClient, make_api, write_home_settings
from .fake_ha_server import FakeHomeAssistant, wait_until

pytestmark = pytest.mark.timeout(30)


def _build(
    server: FakeHomeAssistant,
    client: FakeClient,
    user_config: Path,
    *,
    privacy: PrivacyView | None = None,
    port: int | None = None,
) -> Any:
    from nox_plugin_home import create

    write_home_settings(
        user_config,
        host="127.0.0.1",
        port=port if port is not None else server.port,
        min_backoff_s=0.05,
        max_backoff_s=0.2,
    )
    api = make_api(client, port=port if port is not None else server.port, privacy=privacy)
    return api, create(api)


async def test_available_once_authenticated_with_home_assistant(
    ha_server: FakeHomeAssistant, fake_client: FakeClient, home_config: Path
) -> None:
    api, plugin = _build(ha_server, fake_client, home_config)
    await plugin.start()
    try:
        await wait_until(lambda: plugin.client.authenticated)
        await api.tools.call("home.list", {})  # the area registry is read on the first listing
        status, reason = await plugin.health()
        assert status is HealthStatus.AVAILABLE
        assert "2026.9.0" in reason
    finally:
        await plugin.stop()


async def test_limited_when_the_token_cannot_read_the_area_registry(
    fake_client: FakeClient, home_config: Path
) -> None:
    """A non-admin long-lived token is a normal setup: rooms are unknown, and Nox says so."""
    server = FakeHomeAssistant(admin=False)
    await server.start()
    api, plugin = _build(server, fake_client, home_config)
    await plugin.start()
    try:
        await wait_until(lambda: plugin.client.authenticated)
        result = await api.tools.call("home.list", {})
        assert result["areas_available"] is False
        assert result["areas"] == []
        status, reason = await plugin.health()
        assert status is HealthStatus.LIMITED
        assert "area registry" in reason
    finally:
        await plugin.stop()
        await server.stop()


async def test_unavailable_when_no_access_token_is_stored(
    ha_server: FakeHomeAssistant, home_config: Path
) -> None:
    client = FakeClient(token=None)
    _, plugin = _build(ha_server, client, home_config)
    await plugin.start()
    try:
        await wait_until(lambda: bool(plugin.client.last_error))
        status, reason = await plugin.health()
        assert status is HealthStatus.UNAVAILABLE
        assert "nox/home/access_token" in reason
    finally:
        await plugin.stop()


async def test_unavailable_when_home_assistant_is_not_reachable(
    ha_server: FakeHomeAssistant, fake_client: FakeClient, home_config: Path
) -> None:
    unused = ha_server.port  # a port the fake server is *not* listening on
    api, plugin = _build(ha_server, fake_client, home_config, port=unused + 1)
    await plugin.start()
    try:
        await wait_until(lambda: bool(plugin.client.last_error))
        status, _ = await plugin.health()
        assert status is HealthStatus.UNAVAILABLE
        result = await api.tools.call("home.list", {})
        assert result["ok"] is False
        assert result["connected"] is False
        assert result["entities"] == []
    finally:
        await plugin.stop()


@pytest.mark.parametrize("mode", [PrivacyMode.PRIVATE, PrivacyMode.OFFLINE])
async def test_privacy_modes_block_the_connection_even_on_loopback(
    ha_server: FakeHomeAssistant, fake_client: FakeClient, home_config: Path, mode: PrivacyMode
) -> None:
    api, plugin = _build(ha_server, fake_client, home_config, privacy=PrivacyView(mode))
    await plugin.start()
    try:
        await wait_until(lambda: "privacy mode" in plugin.client.last_error)
        assert ha_server.auth_count == 0
        status, reason = await plugin.health()
        assert status is HealthStatus.UNAVAILABLE
        assert mode.value in reason
        status_result = await api.tools.call("home.status.read", {})
        assert status_result["connected"] is False
        assert mode.value in status_result["reason"]
    finally:
        await plugin.stop()


async def test_leaving_the_privacy_mode_lets_the_connection_come_up(
    ha_server: FakeHomeAssistant, fake_client: FakeClient, home_config: Path
) -> None:
    privacy = PrivacyView(PrivacyMode.PRIVATE)
    _, plugin = _build(ha_server, fake_client, home_config, privacy=privacy)
    await plugin.start()
    try:
        await wait_until(lambda: "privacy mode" in plugin.client.last_error)
        privacy.set(PrivacyMode.BALANCED)
        await wait_until(lambda: plugin.client.authenticated, timeout=10.0)
    finally:
        await plugin.stop()


async def test_panic_drops_the_session_without_touching_a_single_device(
    ha_server: FakeHomeAssistant, fake_client: FakeClient, home_config: Path
) -> None:
    _, plugin = _build(ha_server, fake_client, home_config)
    await plugin.start()
    try:
        await wait_until(lambda: plugin.client.authenticated)
        await fake_client.fire("security.panic", {"origin": "ui"})
        assert plugin.client.authenticated is False
        assert ha_server.service_calls == []
    finally:
        await plugin.stop()

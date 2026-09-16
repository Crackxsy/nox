"""Honest health for the `obs` plugin: AVAILABLE only once actually connected+identified with
events flowing, LIMITED when connected but not subscribed to events, UNAVAILABLE for "no secret"
and "no OBS" - never a faked green."""

from __future__ import annotations

import asyncio

import pytest
from nox_plugin_obs import create

from nox.core.events import HealthStatus

from .conftest import FakeClient, make_api

pytestmark = pytest.mark.timeout(30)


async def _wait_connected(plugin, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if plugin.client.identified:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("plugin never connected to the fake OBS server")


async def test_available_once_connected_and_subscribed(obs_server, fake_client: FakeClient) -> None:
    api = make_api(fake_client, port=obs_server.port)
    plugin = create(api)
    await plugin.start()
    try:
        await _wait_connected(plugin)
        status, reason = await plugin.health()
        assert status is HealthStatus.AVAILABLE
        assert reason == "connected"
    finally:
        await plugin.stop()


async def test_limited_when_events_are_not_subscribed(obs_server, fake_client: FakeClient) -> None:
    api = make_api(fake_client, port=obs_server.port, subscribe_events=False)
    plugin = create(api)
    await plugin.start()
    try:
        await _wait_connected(plugin)
        status, reason = await plugin.health()
        assert status is HealthStatus.LIMITED
        assert "events" in reason
    finally:
        await plugin.stop()


async def test_unavailable_when_no_secret_is_configured(obs_server) -> None:
    client = FakeClient(secret=None)
    api = make_api(client, port=obs_server.port)
    plugin = create(api)
    await plugin.start()
    try:
        await asyncio.sleep(0.3)
        assert plugin.client.identified is False
        status, reason = await plugin.health()
        assert status is HealthStatus.UNAVAILABLE
        assert "no secret" in reason
    finally:
        await plugin.stop()


async def test_unavailable_when_obs_is_not_running(fake_client: FakeClient) -> None:
    api = make_api(fake_client, port=59999)  # nothing listens here
    plugin = create(api)
    await plugin.start()
    try:
        await asyncio.sleep(0.3)
        status, _reason = await plugin.health()
        assert status is HealthStatus.UNAVAILABLE
    finally:
        await plugin.stop()

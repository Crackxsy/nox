"""OBS state is read on every connect, not only learned from change events.

OBS sends `StreamStateChanged` when a stream starts or stops. A stream that was already live when
Nox started - or one that ended while OBS was gone - never produces that event for Nox, so the
stream session, the Funken booking and the clip session id all depended on luck.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from nox_plugin_obs import create

from .conftest import FakeClient, make_api
from .fake_obs_server import FakeObsServer

pytestmark = pytest.mark.timeout(30)


async def _until(predicate: Any, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition never became true")


def _names(client: FakeClient) -> list[str]:
    return [name for name, _ in client.events]


async def test_a_stream_already_live_when_nox_starts_opens_a_session(
    obs_server: FakeObsServer, fake_client: FakeClient
) -> None:
    obs_server.set_response("GetStreamStatus", lambda _d: {"outputActive": True})
    plugin = create(make_api(fake_client, port=obs_server.port))
    await plugin.start()
    try:
        await _until(lambda: "stream.started" in _names(fake_client))
        started = dict(fake_client.events)["stream.started"]
        assert started["session_id"] and started["mode"] == "live"
    finally:
        await plugin.stop()


async def test_a_stream_that_ended_while_obs_was_away_is_closed_on_reconnect(
    obs_server: FakeObsServer, fake_client: FakeClient
) -> None:
    live = {"active": True}
    obs_server.set_response("GetStreamStatus", lambda _d: {"outputActive": live["active"]})
    plugin = create(make_api(fake_client, port=obs_server.port))
    await plugin.start()
    try:
        await _until(lambda: "stream.started" in _names(fake_client))
        live["active"] = False
        await obs_server.disconnect_all()
        await _until(lambda: "stream.ended" in _names(fake_client))
    finally:
        await plugin.stop()


async def test_a_reconnect_during_a_live_stream_keeps_the_same_session(
    obs_server: FakeObsServer, fake_client: FakeClient
) -> None:
    obs_server.set_response("GetStreamStatus", lambda _d: {"outputActive": True})
    plugin = create(make_api(fake_client, port=obs_server.port))
    await plugin.start()
    try:
        await _until(lambda: "stream.started" in _names(fake_client))
        await obs_server.disconnect_all()
        await _until(lambda: obs_server.identify_count >= 2)
        await _until(lambda: _names(fake_client).count("obs.connected") >= 2)
        await asyncio.sleep(0.2)
        assert _names(fake_client).count("stream.started") == 1
        assert "stream.ended" not in _names(fake_client)
    finally:
        await plugin.stop()

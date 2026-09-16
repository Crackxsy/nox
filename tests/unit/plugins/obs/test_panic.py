"""`security.panic` -> the configured privacy scene, without the normal `confirm` step (Spec v0.2
§3.6.3): the handler talks to the OBS client directly, never through `api.tools.call`/the
permission engine."""

from __future__ import annotations

import asyncio

import pytest
from nox_plugin_obs import create

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


async def test_panic_switches_to_the_privacy_scene_without_confirmation(
    obs_server, fake_client: FakeClient
) -> None:
    api = make_api(fake_client, port=obs_server.port)
    plugin = create(api)
    await plugin.start()
    try:
        await _wait_connected(plugin)
        await fake_client.fire("security.panic", {"by": "hotkey", "reason": "test"})
        await asyncio.sleep(0.1)
        assert obs_server.requests[-1] == (
            "SetCurrentProgramScene",
            {"sceneName": "Technical Problems"},
        )
    finally:
        await plugin.stop()


async def test_panic_never_touches_stream_stop_or_recording_delete(
    obs_server, fake_client: FakeClient
) -> None:
    api = make_api(fake_client, port=obs_server.port)
    plugin = create(api)
    await plugin.start()
    try:
        await _wait_connected(plugin)
        await fake_client.fire("security.panic", {"by": "hotkey", "reason": "test"})
        await asyncio.sleep(0.1)
        assert all(name != "StopStream" for name, _ in obs_server.requests)
        assert all(
            "Record" not in name or name == "GetRecordStatus" for name, _ in obs_server.requests
        )
    finally:
        await plugin.stop()


async def test_panic_without_a_configured_privacy_scene_only_logs(
    obs_server, fake_client: FakeClient
) -> None:
    api = make_api(fake_client, port=obs_server.port, privacy_scene="")
    plugin = create(api)
    await plugin.start()
    try:
        await _wait_connected(plugin)
        await fake_client.fire("security.panic", {"by": "hotkey", "reason": "test"})
        await asyncio.sleep(0.1)
        assert all(name != "SetCurrentProgramScene" for name, _ in obs_server.requests)
    finally:
        await plugin.stop()


async def test_panic_survives_obs_being_unreachable(fake_client: FakeClient) -> None:
    """§3.6.3: if OBS is unreachable, panic does not attempt to stop the stream and does not
    raise - it only reports the failure (here: a log call, asserted not to blow up the handler)."""
    api = make_api(fake_client, port=1)  # nothing listens on port 1
    plugin = create(api)
    await plugin.start()
    try:
        await fake_client.fire("security.panic", {"by": "hotkey", "reason": "test"})
        await asyncio.sleep(0.1)  # must not raise
    finally:
        await plugin.stop()

"""`obs.*` tool handlers, registered through `api.tools.register` exactly like every other plugin
(`create(api)` -> `api.tools.call(name, payload)`), against the fake OBS server."""

from __future__ import annotations

import asyncio

import pytest
from nox_plugin_obs import create

from .conftest import FakeClient, make_api
from .fake_obs_server import default_scene_list

pytestmark = pytest.mark.timeout(30)


async def _wait_connected(plugin, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if plugin.client.identified:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("plugin never connected to the fake OBS server")


async def _plugin(obs_server, fake_client: FakeClient, **config_overrides):
    api = make_api(fake_client, port=obs_server.port, **config_overrides)
    plugin = create(api)
    await plugin.start()
    await _wait_connected(plugin)
    return api, plugin


async def test_tools_are_registered_with_the_manifest_risk(
    obs_server, fake_client: FakeClient
) -> None:
    api, plugin = await _plugin(obs_server, fake_client)
    try:
        names = api.tools.names()
        assert names == sorted(
            [
                "obs.status.read",
                "obs.scenes.list",
                "obs.scene.switch",
                "obs.privacy_scene.activate",
                "obs.preflight.check",
                "obs.replay_buffer.save",
                "obs.replay_buffer.status.read",
            ]
        )
        assert api.tools.get("obs.scene.switch").risk.value == "medium"
        assert api.tools.get("obs.status.read").risk.value == "read"
    finally:
        await plugin.stop()


async def test_status_read_reports_the_full_snapshot(obs_server, fake_client: FakeClient) -> None:
    obs_server.set_response(
        "GetSceneList", lambda _d: default_scene_list(["Start", "Live"], "Live")
    )
    obs_server.set_response("GetStreamStatus", lambda _d: {"outputActive": True})
    api, plugin = await _plugin(obs_server, fake_client)
    try:
        result = await api.tools.call("obs.status.read", {})
        assert result == {
            "connected": True,
            "current_scene": "Live",
            "scenes": ["Start", "Live"],
            "streaming": True,
            "recording": False,
        }
    finally:
        await plugin.stop()


async def test_scenes_list(obs_server, fake_client: FakeClient) -> None:
    api, plugin = await _plugin(obs_server, fake_client)
    try:
        result = await api.tools.call("obs.scenes.list", {})
        assert result["current_scene"] == "Start"
        assert result["scenes"] == ["Start", "Live"]
    finally:
        await plugin.stop()


async def test_scene_switch_sends_set_current_program_scene(
    obs_server, fake_client: FakeClient
) -> None:
    api, plugin = await _plugin(obs_server, fake_client)
    try:
        result = await api.tools.call("obs.scene.switch", {"target": "Live"})
        assert result == {"target": "Live", "ok": True}
        assert obs_server.requests[-1] == ("SetCurrentProgramScene", {"sceneName": "Live"})
    finally:
        await plugin.stop()


async def test_scene_switch_rejects_a_scene_outside_the_configured_set(
    obs_server, fake_client: FakeClient
) -> None:
    api, plugin = await _plugin(obs_server, fake_client)
    try:
        with pytest.raises(Exception, match="not in the configured Nox scene set"):
            await api.tools.call("obs.scene.switch", {"target": "Basement"})
    finally:
        await plugin.stop()


async def test_privacy_scene_activate_switches_to_the_configured_scene(
    obs_server, fake_client: FakeClient
) -> None:
    api, plugin = await _plugin(obs_server, fake_client)
    try:
        result = await api.tools.call("obs.privacy_scene.activate", {})
        assert result == {"target": "Technical Problems", "ok": True}
        assert obs_server.requests[-1] == (
            "SetCurrentProgramScene",
            {"sceneName": "Technical Problems"},
        )
    finally:
        await plugin.stop()


async def test_preflight_check_is_honest_about_what_obs_cannot_answer(
    obs_server, fake_client: FakeClient
) -> None:
    obs_server.set_response(
        "GetSceneList",
        lambda _d: default_scene_list(["Start", "Live", "Pause", "Technical Problems"], "Start"),
    )
    api, plugin = await _plugin(obs_server, fake_client)
    try:
        result = await api.tools.call("obs.preflight.check", {})
        by_name = {item["name"]: item for item in result["items"]}
        assert by_name["obs_websocket"]["status"] == "green"
        assert by_name["scene_set"]["status"] == "green"
        for name in ("twitch_token", "mic_level", "camera", "nox_health", "ai_budget", "network"):
            assert by_name[name]["status"] == "unknown"
        assert result["overall"] == "green"
    finally:
        await plugin.stop()


async def test_preflight_check_flags_a_missing_scene(obs_server, fake_client: FakeClient) -> None:
    obs_server.set_response(
        "GetSceneList", lambda _d: default_scene_list(["Start", "Live"], "Start")
    )
    api, plugin = await _plugin(obs_server, fake_client)
    try:
        result = await api.tools.call("obs.preflight.check", {})
        by_name = {item["name"]: item for item in result["items"]}
        assert by_name["scene_set"]["status"] == "amber"
        assert result["overall"] == "amber"
    finally:
        await plugin.stop()


# ---- ST-15-02: obs.replay_buffer.save / .status.read (Spec v0.6 Clip Pipeline) -----------------


async def test_replay_buffer_status_read_reports_active(
    obs_server, fake_client: FakeClient
) -> None:
    api, plugin = await _plugin(obs_server, fake_client)
    try:
        result = await api.tools.call("obs.replay_buffer.status.read", {})
        assert result == {"active": True, "reason": ""}
    finally:
        await plugin.stop()


async def test_replay_buffer_save_resolves_the_written_path(
    obs_server, fake_client: FakeClient
) -> None:
    api, plugin = await _plugin(obs_server, fake_client)
    try:
        call = asyncio.ensure_future(api.tools.call("obs.replay_buffer.save", {}))
        await asyncio.sleep(0.05)  # let SaveReplayBuffer land before the event fires
        await obs_server.replay_buffer_saved(r"E:\Nox\data\clips\incoming\replay_001.mp4")
        result = await asyncio.wait_for(call, timeout=5.0)
        assert result == {
            "ok": True,
            "file_path": r"E:\Nox\data\clips\incoming\replay_001.mp4",
            "reason": "",
        }
        assert ("SaveReplayBuffer", {}) in obs_server.requests
    finally:
        await plugin.stop()


async def test_replay_buffer_save_fails_cleanly_when_buffer_is_disabled(
    obs_server, fake_client: FakeClient
) -> None:
    obs_server.set_response("GetReplayBufferStatus", lambda _d: {"outputActive": False})
    api, plugin = await _plugin(obs_server, fake_client)
    try:
        result = await api.tools.call("obs.replay_buffer.save", {})
        assert result["ok"] is False
        assert result["file_path"] is None
        assert "not enabled" in result["reason"] or "not active" in result["reason"]
        # never a raised exception into the caller - a structured, non-crashing result instead
    finally:
        await plugin.stop()


async def test_replay_buffer_save_times_out_when_obs_never_reports_the_event(
    obs_server, fake_client: FakeClient
) -> None:
    api, plugin = await _plugin(obs_server, fake_client, replay_save_timeout_s=0.1)
    try:
        result = await asyncio.wait_for(api.tools.call("obs.replay_buffer.save", {}), timeout=5.0)
        assert result["ok"] is False
        assert result["file_path"] is None
        assert "time" in result["reason"]
    finally:
        await plugin.stop()

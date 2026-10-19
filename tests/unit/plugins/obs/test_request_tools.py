"""`obs.request`, `obs.screenshot`, `obs.overlay.setup` and `obs.audio_track.setup` against the
fake obs-websocket server, plus the connect-time stream state and the auth-failure report. The
permission side (which request types run, ask or are refused) is covered through the executor in
`tests/unit/tools/test_executor_scope.py`; here the handler's own refusal is the second lock."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

import pytest
from nox_plugin_obs import create
from nox_plugin_obs.plugin import MAX_RESPONSE_CHARS, SCREENSHOTS_KEPT

from nox.core.events import HealthStatus
from nox.plugins.api import PluginApiError

from .conftest import FakeClient, make_api
from .fake_obs_server import FakeObsServer

pytestmark = pytest.mark.timeout(30)

OVERLAY_URL = "http://127.0.0.1:47801/pet/?overlay=1#overlay=abc"


async def _until(condition: Any, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition never became true")


async def _plugin(server: FakeObsServer, client: FakeClient, **config: Any) -> Any:
    api = make_api(client, port=server.port, **config)
    plugin = create(api)
    await plugin.start()
    await _until(lambda: plugin.client.identified)
    return api, plugin


def _types(server: FakeObsServer) -> list[str]:
    return [t for t, _ in server.requests]


# ---- obs.request ---------------------------------------------------------------------------


async def test_request_returns_obs_response(
    obs_server: FakeObsServer, fake_client: FakeClient
) -> None:
    obs_server.set_response("GetInputList", lambda _d: {"inputs": [{"inputName": "Cam"}]})
    api, plugin = await _plugin(obs_server, fake_client)
    try:
        result = await api.tools.call(
            "obs.request", {"request_type": "GetInputList", "request_data": {"inputKind": "x"}}
        )
        assert result == {
            "ok": True,
            "request_type": "GetInputList",
            "response": {"inputs": [{"inputName": "Cam"}]},
        }
        assert ("GetInputList", {"inputKind": "x"}) in obs_server.requests
    finally:
        await plugin.stop()


async def test_request_reports_obs_errors_as_results(
    obs_server: FakeObsServer, fake_client: FakeClient
) -> None:
    obs_server.set_response("CreateScene", RuntimeError("scene already exists"))
    api, plugin = await _plugin(obs_server, fake_client)
    try:
        result = await api.tools.call(
            "obs.request", {"request_type": "CreateScene", "request_data": {"sceneName": "A"}}
        )
        assert result["ok"] is False and "already exists" in result["comment"]
    finally:
        await plugin.stop()


@pytest.mark.parametrize(
    "request_type", ["StopStream", "GetStreamServiceSettings", "TriggerHotkeyByName"]
)
async def test_handler_refuses_what_the_core_never_allows(
    obs_server: FakeObsServer, fake_client: FakeClient, request_type: str
) -> None:
    api, plugin = await _plugin(obs_server, fake_client)
    try:
        with pytest.raises(PermissionError):
            await api.tools.call("obs.request", {"request_type": request_type})
        assert request_type not in _types(obs_server)
    finally:
        await plugin.stop()


async def test_request_type_must_look_like_one(
    obs_server: FakeObsServer, fake_client: FakeClient
) -> None:
    api, plugin = await _plugin(obs_server, fake_client)
    try:
        with pytest.raises(PluginApiError):
            await api.tools.call("obs.request", {"request_type": "getSceneList"})
    finally:
        await plugin.stop()


async def test_large_responses_are_cut(obs_server: FakeObsServer, fake_client: FakeClient) -> None:
    obs_server.set_response("GetInputSettings", lambda _d: {"blob": "x" * (MAX_RESPONSE_CHARS * 2)})
    api, plugin = await _plugin(obs_server, fake_client)
    try:
        result = await api.tools.call("obs.request", {"request_type": "GetInputSettings"})
        assert result["truncated"] is True
        assert len(result["response_preview"]) == MAX_RESPONSE_CHARS
        assert "response" not in result
    finally:
        await plugin.stop()


# ---- obs.screenshot ------------------------------------------------------------------------


async def test_screenshot_is_stored_locally_and_never_returned(
    obs_server: FakeObsServer, fake_client: FakeClient, tmp_path: Path
) -> None:
    image = b"\xff\xd8jpeg-bytes"
    encoded = base64.b64encode(image).decode()
    obs_server.set_response("GetCurrentProgramScene", {"currentProgramSceneName": "Live"})
    obs_server.set_response(
        "GetSourceScreenshot", lambda _d: {"imageData": f"data:image/jpg;base64,{encoded}"}
    )
    api, plugin = await _plugin(obs_server, fake_client, screenshot_dir=str(tmp_path))
    try:
        for _ in range(SCREENSHOTS_KEPT + 2):
            result = await api.tools.call("obs.screenshot", {"width": 320})
        assert result["source"] == "Live" and result["bytes"] == len(image)
        assert encoded not in str(result)
        stored = await asyncio.to_thread(Path(result["path"]).read_bytes)
        assert stored == image
        kept = await asyncio.to_thread(lambda: list(tmp_path.glob("obs-*.jpg")))
        assert len(kept) == SCREENSHOTS_KEPT
        sent = dict(obs_server.requests)["GetSourceScreenshot"]
        assert sent["sourceName"] == "Live" and sent["imageWidth"] == 320
    finally:
        await plugin.stop()


# ---- obs.overlay.setup ---------------------------------------------------------------------


async def test_overlay_is_created_in_the_current_scene(
    obs_server: FakeObsServer, fake_client: FakeClient
) -> None:
    obs_server.set_response("GetCurrentProgramScene", {"currentProgramSceneName": "Live"})
    obs_server.set_response("GetInputList", {"inputs": []})
    api, plugin = await _plugin(obs_server, fake_client)
    try:
        result = await api.tools.call("obs.overlay.setup", {"url": OVERLAY_URL})
        assert result == {
            "input_name": "Nox Overlay",
            "scene": "Live",
            "created": True,
            "added": True,
        }
        created = dict(obs_server.requests)["CreateInput"]
        assert created["inputKind"] == "browser_source" and created["sceneName"] == "Live"
        assert created["inputSettings"]["url"] == OVERLAY_URL
        assert "rgba(0, 0, 0, 0)" in created["inputSettings"]["css"]
    finally:
        await plugin.stop()


async def test_overlay_is_updated_in_place_and_added_where_missing(
    obs_server: FakeObsServer, fake_client: FakeClient
) -> None:
    obs_server.set_response("GetCurrentProgramScene", {"currentProgramSceneName": "Live"})
    obs_server.set_response("GetInputList", {"inputs": [{"inputName": "Nox Overlay"}]})
    obs_server.set_response("GetSceneItemId", RuntimeError("no such item"))
    api, plugin = await _plugin(obs_server, fake_client)
    try:
        result = await api.tools.call("obs.overlay.setup", {"url": OVERLAY_URL})
        assert result["created"] is False and result["added"] is True
        types = _types(obs_server)
        assert "CreateInput" not in types
        assert types.index("SetInputSettings") < types.index("CreateSceneItem")
    finally:
        await plugin.stop()


async def test_overlay_never_goes_into_the_privacy_scene(
    obs_server: FakeObsServer, fake_client: FakeClient
) -> None:
    obs_server.set_response(
        "GetCurrentProgramScene", {"currentProgramSceneName": "Technical Problems"}
    )
    api, plugin = await _plugin(obs_server, fake_client)
    try:
        with pytest.raises(PermissionError):
            await api.tools.call("obs.overlay.setup", {"url": OVERLAY_URL})
        assert "CreateInput" not in _types(obs_server)
    finally:
        await plugin.stop()


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/pet/#overlay=abc",
        "http://example.com/pet/#overlay=abc",
        "http://127.0.0.1:47801/dashboard/#token=abc",
    ],
)
async def test_overlay_url_must_be_noxs_own_pet_page(
    obs_server: FakeObsServer, fake_client: FakeClient, url: str
) -> None:
    api, plugin = await _plugin(obs_server, fake_client)
    try:
        with pytest.raises(PluginApiError):
            await api.tools.call("obs.overlay.setup", {"url": url})
    finally:
        await plugin.stop()


# ---- obs.audio_track.setup -----------------------------------------------------------------

DEVICES = {
    "propertyItems": [
        {"itemName": "Default", "itemValue": "default", "itemEnabled": True},
        {
            "itemName": "CABLE Output (VB-Audio Virtual Cable)",
            "itemValue": "{cable}",
            "itemEnabled": True,
        },
    ]
}


async def test_audio_track_captures_the_virtual_cable(
    obs_server: FakeObsServer, fake_client: FakeClient
) -> None:
    obs_server.set_response("GetCurrentProgramScene", {"currentProgramSceneName": "Live"})
    obs_server.set_response("GetInputList", {"inputs": []})
    obs_server.set_response("GetInputPropertiesListPropertyItems", DEVICES)
    api, plugin = await _plugin(obs_server, fake_client)
    try:
        result = await api.tools.call("obs.audio_track.setup", {"tracks": [3]})
        assert result["ok"] is True and result["device"].startswith("CABLE Output")
        sent = dict(obs_server.requests)
        assert sent["CreateInput"]["inputKind"] == "wasapi_input_capture"
        assert sent["SetInputSettings"] == {
            "inputName": "Nox Voice",
            "inputSettings": {"device_id": "{cable}"},
        }
        tracks = sent["SetInputAudioTracks"]["inputAudioTracks"]
        assert tracks == {"1": False, "2": False, "3": True, "4": False, "5": False, "6": False}
    finally:
        await plugin.stop()


async def test_audio_track_without_a_matching_device_leaves_nothing_behind(
    obs_server: FakeObsServer, fake_client: FakeClient
) -> None:
    obs_server.set_response("GetCurrentProgramScene", {"currentProgramSceneName": "Live"})
    obs_server.set_response("GetInputList", {"inputs": []})
    obs_server.set_response(
        "GetInputPropertiesListPropertyItems",
        {"propertyItems": [{"itemName": "Mic", "itemValue": "{mic}", "itemEnabled": True}]},
    )
    api, plugin = await _plugin(obs_server, fake_client)
    try:
        result = await api.tools.call("obs.audio_track.setup", {})
        assert result["ok"] is False and result["devices"] == ["Mic"]
        assert dict(obs_server.requests)["RemoveInput"] == {"inputName": "Nox Voice"}
        assert "SetInputSettings" not in _types(obs_server)
    finally:
        await plugin.stop()


# ---- connection-time behaviour -------------------------------------------------------------


async def test_already_live_obs_starts_the_session_on_connect(
    obs_server: FakeObsServer, fake_client: FakeClient
) -> None:
    obs_server.set_response("GetStreamStatus", {"outputActive": True})
    _api, plugin = await _plugin(obs_server, fake_client)
    try:
        await _until(lambda: any(n == "stream.started" for n, _ in fake_client.events))
        assert [n for n, _ in fake_client.events].count("stream.started") == 1
    finally:
        await plugin.stop()


async def test_wrong_password_is_reported_once_and_in_health() -> None:
    server = FakeObsServer(password="right")
    await server.start()
    client = FakeClient(secret="wrong")
    api = make_api(client, port=server.port)
    plugin = create(api)
    await plugin.start()
    try:
        await _until(lambda: plugin.client.backoff_s > 0.1 and plugin.client.auth_failed)
        await _until(lambda: any(n == "obs.auth_failed" for n, _ in client.events))
        # Several failed attempts by now (backoff grew), one report.
        assert [n for n, _ in client.events].count("obs.auth_failed") == 1
        status, reason = await plugin.health()
        assert status is HealthStatus.UNAVAILABLE and "rejected" in reason
    finally:
        await plugin.stop()
        await server.stop()

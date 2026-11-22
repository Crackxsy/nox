"""Contract tests: Nox's obs-websocket client (`nox.stream.obs_ws`) and the OBS plugin built on it,
against the workshop's fake OBS - a protocol implementation written without them.

Each test names the promise it checks: what the client sends and does when OBS behaves, and what
it does when OBS does not (a wrong password, an answer that never comes, a connection cut in the
middle of a request, a server that refuses for a while). The fake is checked against the
specification in `test_fake_obs.py`; here the thing under test is Nox.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from nox_plugin_obs import create
from nox_plugin_obs.plugin import ObsPlugin

from nox.core.events import HealthStatus
from nox.stream.obs_ws import (
    DEFAULT_EVENT_SUBSCRIPTIONS,
    EVENT_SUB_OUTPUTS,
    ObsRequestError,
    ObsWebSocketClient,
)
from tests.unit.fakes import wait_until
from tests.unit.plugins.obs.conftest import FakeClient, make_api
from tests.workshop.fake_obs import FakeObs, Status
from tests.workshop.fake_obs.protocol import SUB_GENERAL, SUB_OUTPUTS, SUB_SCENES

pytestmark = pytest.mark.timeout(30)

PASSWORD = "correct-horse"
MIN_BACKOFF_S = 0.05
MAX_BACKOFF_S = 0.2


# -- the client ---------------------------------------------------------------------------------


class Recorded:
    """A client plus what its hooks saw."""

    def __init__(self, client: ObsWebSocketClient) -> None:
        self.client = client
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.connected = 0
        self.disconnected: list[str] = []
        self.auth_failed: list[str] = []


def _make(
    obs: FakeObs,
    password: str | None = PASSWORD,
    **overrides: Any,
) -> Recorded:
    holder: list[Recorded] = []

    async def provider() -> str | None:
        return password

    async def on_event(name: str, data: dict[str, Any]) -> None:
        holder[0].events.append((name, data))

    async def on_connected() -> None:
        holder[0].connected += 1

    async def on_disconnected(reason: str) -> None:
        holder[0].disconnected.append(reason)

    async def on_auth_failed(reason: str) -> None:
        holder[0].auth_failed.append(reason)

    options: dict[str, Any] = {
        "min_backoff_s": MIN_BACKOFF_S,
        "max_backoff_s": MAX_BACKOFF_S,
        "request_timeout_s": 5.0,
        **overrides,
    }
    client = ObsWebSocketClient(
        obs.url,
        password_provider=provider,
        on_event=on_event,
        on_connected=on_connected,
        on_disconnected=on_disconnected,
        on_auth_failed=on_auth_failed,
        **options,
    )
    holder.append(Recorded(client))
    return holder[0]


@pytest.fixture
async def running(obs: FakeObs) -> AsyncIterator[Recorded]:
    recorded = _make(obs, password=None)
    recorded.client.start()
    try:
        await wait_until(lambda: recorded.client.identified, what="the client to identify")
        yield recorded
    finally:
        await recorded.client.stop()


async def test_the_client_identifies_with_rpc_version_1_and_the_default_subscriptions(
    obs: FakeObs, running: Recorded
) -> None:
    (identify,) = obs.identifies
    assert identify.rpc_version == 1
    assert identify.event_subscriptions == DEFAULT_EVENT_SUBSCRIPTIONS
    assert identify.event_subscriptions == SUB_GENERAL | SUB_SCENES | SUB_OUTPUTS
    assert identify.authenticated is False
    assert running.connected == 1


async def test_the_client_answers_the_challenge_with_the_documented_hash(
    obs_with_password: FakeObs,
) -> None:
    recorded = _make(obs_with_password, PASSWORD)
    recorded.client.start()
    try:
        await wait_until(lambda: recorded.client.identified, what="authentication")
    finally:
        await recorded.client.stop()
    assert [i.authenticated for i in obs_with_password.identifies] == [True]


async def test_a_wrong_password_is_reported_once_and_never_identifies(
    obs_with_password: FakeObs,
) -> None:
    recorded = _make(obs_with_password, "not-the-password")
    recorded.client.start()
    try:
        # Several attempts, one report: the user is told once, the client keeps trying quietly.
        await wait_until(
            lambda: obs_with_password.connection_count >= 3, what="three login attempts"
        )
        assert recorded.client.auth_failed is True
        assert len(recorded.auth_failed) == 1
        assert "password" in recorded.auth_failed[0]
        assert recorded.client.identified is False
        assert obs_with_password.identifies == []
    finally:
        await recorded.client.stop()


async def test_a_missing_password_sends_no_identify_at_all(obs_with_password: FakeObs) -> None:
    recorded = _make(obs_with_password, None)
    recorded.client.start()
    try:
        await wait_until(lambda: recorded.auth_failed, what="the report")
        assert "websocket_password" in recorded.auth_failed[0]
        assert obs_with_password.identifies == []
    finally:
        await recorded.client.stop()


async def test_the_client_recovers_once_the_password_is_right(
    obs_with_password: FakeObs,
) -> None:
    password: list[str | None] = ["wrong"]

    async def provider() -> str | None:
        return password[0]

    client = ObsWebSocketClient(
        obs_with_password.url,
        password_provider=provider,
        on_event=lambda *_: None,
        min_backoff_s=MIN_BACKOFF_S,
        max_backoff_s=MAX_BACKOFF_S,
    )
    client.start()
    try:
        await wait_until(lambda: client.auth_failed, what="the first rejection")
        password[0] = PASSWORD
        await wait_until(lambda: client.identified, what="the client to identify")
        assert client.auth_failed is False
        assert client.last_error == ""
    finally:
        await client.stop()


async def test_only_subscribed_event_categories_arrive(obs: FakeObs) -> None:
    recorded = _make(obs, None, event_subscriptions=EVENT_SUB_OUTPUTS)
    recorded.client.start()
    try:
        await wait_until(lambda: recorded.client.identified, what="the client to identify")
        await obs.emit("CurrentProgramSceneChanged", {"sceneName": "Live"})  # not subscribed
        await obs.emit("StreamStateChanged", {"outputActive": True, "outputState": "STARTED"})
        await wait_until(lambda: recorded.events, what="the stream event")
    finally:
        await recorded.client.stop()
    assert [name for name, _ in recorded.events] == ["StreamStateChanged"]
    assert obs.identifies[0].event_subscriptions == SUB_OUTPUTS


async def test_a_request_returns_the_response_data(obs: FakeObs, running: Recorded) -> None:
    data = await running.client.request("GetSceneList")
    assert data["currentProgramSceneName"] == "Start"
    assert [s["sceneName"] for s in data["scenes"]] == list(obs.state.scene_names)


async def test_a_request_carries_its_data_and_changes_the_state(
    obs: FakeObs, running: Recorded
) -> None:
    await running.client.request("SetCurrentProgramScene", {"sceneName": "Live"})
    assert obs.requests_of("SetCurrentProgramScene") == [{"sceneName": "Live"}]
    assert obs.state.current_scene == "Live"
    await wait_until(lambda: running.events, what="the scene event")
    assert running.events == [
        (
            "CurrentProgramSceneChanged",
            {"sceneName": "Live"},
        )
    ]


async def test_a_request_without_data_sends_no_request_data_field(
    obs: FakeObs, running: Recorded
) -> None:
    await running.client.request("GetStats")
    assert obs.requests_of("GetStats") == [{}]


async def test_a_refused_request_raises_with_the_code_and_comment_from_obs(
    obs: FakeObs, running: Recorded
) -> None:
    with pytest.raises(ObsRequestError) as caught:
        await running.client.request("SetCurrentProgramScene", {"sceneName": "Nowhere"})
    assert caught.value.code == Status.RESOURCE_NOT_FOUND
    assert "Nowhere" in caught.value.comment
    assert caught.value.request_type == "SetCurrentProgramScene"
    assert obs.state.current_scene == "Start"


async def test_answers_are_matched_to_their_requests_not_to_their_order(
    obs: FakeObs, running: Recorded
) -> None:
    gate = obs.hold("GetStats")
    slow = asyncio.create_task(running.client.request("GetStats"))
    await asyncio.wait_for(obs.arrived("GetStats").wait(), 5)
    fast = await running.client.request("GetVersion")  # answered while GetStats is pending
    assert fast["obsWebSocketVersion"] == "5.5.4"
    assert not slow.done()
    gate.set()
    assert (await slow)["activeFps"] == 60.0


async def test_a_request_that_is_never_answered_times_out_and_the_client_carries_on(
    obs: FakeObs,
) -> None:
    obs.ignore("GetVersion")
    recorded = _make(obs, None, request_timeout_s=0.2)
    recorded.client.start()
    try:
        await wait_until(lambda: recorded.client.identified, what="the client to identify")
        with pytest.raises(TimeoutError):
            await recorded.client.request("GetVersion")
        assert recorded.client._pending == {}  # the dead request left nothing behind
        # The next request gets a long timeout of its own: a slow runner must not fail it here.
        assert (await recorded.client.request("GetStats", timeout=5.0))["activeFps"] == 60.0
    finally:
        await recorded.client.stop()


async def test_a_request_before_the_connection_is_up_fails_at_once() -> None:
    async with FakeObs() as obs:
        recorded = _make(obs, None)
        with pytest.raises(ConnectionError, match="not connected to OBS"):
            await recorded.client.request("GetVersion")
    assert obs.requests == []


async def test_a_connection_cut_during_a_request_fails_it_and_the_client_reconnects(
    obs: FakeObs, running: Recorded
) -> None:
    obs.drop_on("GetVersion")
    with pytest.raises(ConnectionError):
        await running.client.request("GetVersion")
    await wait_until(lambda: len(obs.identifies) == 2, what="the second Identify")
    await wait_until(lambda: running.client.identified, what="the client to identify again")
    assert running.connected == 2
    assert len(running.disconnected) == 1
    assert (await running.client.request("GetStats"))["activeFps"] == 60.0
    # Cut and then reconnected: the request was not repeated on the new connection.
    assert obs.requests_of("GetVersion") == [{}]


async def test_events_keep_arriving_after_a_reconnect(obs: FakeObs, running: Recorded) -> None:
    await obs.drop_connections()
    await wait_until(lambda: len(obs.identifies) == 2, what="the reconnect")
    await wait_until(lambda: running.client.identified, what="the client to identify again")
    await obs.emit("StreamStateChanged", {"outputActive": True, "outputState": "STARTED"})
    await wait_until(lambda: running.events, what="an event on the new connection")
    assert running.events[0][0] == "StreamStateChanged"


async def test_the_wait_between_attempts_doubles_up_to_the_ceiling_and_the_client_comes_back(
    obs: FakeObs,
) -> None:
    obs.refuse_connections(5)
    recorded = _make(obs, None)
    recorded.client.start()
    try:
        await wait_until(lambda: recorded.client.identified, what="the client to get in")
        assert obs.connection_count == 6  # five refusals, then the connection that held
        # 0.05 -> 0.1 -> 0.2 -> 0.2 -> 0.2: grown to the ceiling and not past it.
        assert recorded.client.backoff_s == MAX_BACKOFF_S
        assert recorded.disconnected == []  # it never was connected before
    finally:
        await recorded.client.stop()


async def test_while_obs_refuses_the_reason_is_kept_where_a_user_can_see_it(
    obs: FakeObs,
) -> None:
    obs.refuse_connections(10_000)
    recorded = _make(obs, None)
    recorded.client.start()
    try:
        await wait_until(lambda: recorded.client.last_error, what="the reason")
        assert "1013" in recorded.client.last_error
        assert recorded.client.identified is False
    finally:
        await recorded.client.stop()


async def test_a_clean_close_by_obs_is_a_disconnect_and_a_reconnect(
    obs: FakeObs, running: Recorded
) -> None:
    await obs.close_connections(1001, "OBS is closing")
    await wait_until(lambda: len(running.disconnected) == 1, what="the disconnect report")
    await wait_until(lambda: len(obs.identifies) == 2, what="the reconnect")
    assert running.client.backoff_s == MIN_BACKOFF_S  # a clean close does not count as failing


async def test_stopping_the_client_while_obs_never_says_hello_does_not_hang(
    obs: FakeObs,
) -> None:
    obs.stall_hello()
    recorded = _make(obs, None)
    recorded.client.start()
    await wait_until(lambda: obs.connection_count == 1, what="the connection")
    await asyncio.wait_for(recorded.client.stop(), 5)
    assert recorded.client.identified is False


async def test_stopping_the_client_stops_the_attempts(obs: FakeObs) -> None:
    obs.refuse_connections(1000)
    recorded = _make(obs, None)
    recorded.client.start()
    await wait_until(lambda: obs.connection_count >= 2, what="a second attempt")
    await recorded.client.stop()
    attempts = obs.connection_count
    probe = _make(obs, None)  # an unrelated connection proves the server still counts
    probe.client.start()
    await wait_until(lambda: obs.connection_count > attempts, what="the probe's attempt")
    await probe.client.stop()
    assert obs.connection_count - attempts <= 2  # the probe's own, no stray reconnects


# -- the plugin ---------------------------------------------------------------------------------


@pytest.fixture
async def plugin_env(obs: FakeObs) -> AsyncIterator[tuple[ObsPlugin, FakeClient, Any]]:
    client = FakeClient(secret=None)
    api = make_api(client, port=obs.port, replay_save_timeout_s=2.0)
    plugin = create(api)
    await plugin.start()
    await wait_until(lambda: plugin.client.identified, what="the plugin to connect")
    try:
        yield plugin, client, api
    finally:
        await plugin.stop()


def _events(client: FakeClient, name: str) -> list[dict[str, Any]]:
    return [payload for event, payload in client.events if event == name]


async def test_the_plugin_announces_the_connection_and_reads_the_status_from_obs(
    obs: FakeObs, plugin_env: tuple[ObsPlugin, FakeClient, Any]
) -> None:
    plugin, client, api = plugin_env
    assert _events(client, "obs.connected") == [{"reason": ""}]
    assert await api.tools.call("obs.status.read", {}) == {
        "connected": True,
        "current_scene": "Start",
        "scenes": list(obs.state.scene_names),
        "streaming": False,
        "recording": False,
    }
    health, _reason = await plugin.health()
    assert health is HealthStatus.AVAILABLE


async def test_switching_the_scene_changes_obs_and_is_announced_from_the_event(
    obs: FakeObs, plugin_env: tuple[ObsPlugin, FakeClient, Any]
) -> None:
    _plugin, client, api = plugin_env
    assert await api.tools.call("obs.scene.switch", {"target": "Live"}) == {
        "target": "Live",
        "ok": True,
    }
    assert obs.state.current_scene == "Live"
    await wait_until(lambda: _events(client, "obs.scene_changed"), what="the scene event")
    assert _events(client, "obs.scene_changed") == [
        {"previous_scene": "", "current_scene": "Live", "by": "manual"}
    ]


async def test_a_scene_outside_the_allowed_set_never_reaches_obs(
    obs: FakeObs, plugin_env: tuple[ObsPlugin, FakeClient, Any]
) -> None:
    _plugin, _client, api = plugin_env
    obs.state.scenes["Secret"] = []
    with pytest.raises(ValueError, match="not in the configured Nox scene set"):
        await api.tools.call("obs.scene.switch", {"target": "Secret"})
    assert obs.requests_of("SetCurrentProgramScene") == []
    assert obs.state.current_scene == "Start"


async def test_a_scene_obs_does_not_have_comes_back_as_obs_says_it(
    obs: FakeObs, plugin_env: tuple[ObsPlugin, FakeClient, Any]
) -> None:
    _plugin, _client, api = plugin_env
    del obs.state.scenes["Pause"]  # allowed by the plugin's set, gone in OBS
    with pytest.raises(ObsRequestError) as caught:
        await api.tools.call("obs.scene.switch", {"target": "Pause"})
    assert caught.value.code == Status.RESOURCE_NOT_FOUND


async def test_a_source_made_through_obs_request_exists_in_obs(
    obs: FakeObs, plugin_env: tuple[ObsPlugin, FakeClient, Any]
) -> None:
    _plugin, _client, api = plugin_env
    result = await api.tools.call(
        "obs.request",
        {
            "request_type": "CreateInput",
            "request_data": {
                "sceneName": "Live",
                "inputName": "Notes",
                "inputKind": "text_gdiplus_v3",
                "inputSettings": {"text": "Hallo"},
            },
        },
    )
    assert result["ok"] is True
    assert obs.state.inputs["Notes"].kind == "text_gdiplus_v3"
    assert [i.source_name for i in obs.state.scenes["Live"]] == ["Notes"]


async def test_a_refusal_from_obs_is_a_result_the_model_can_read(
    obs: FakeObs, plugin_env: tuple[ObsPlugin, FakeClient, Any]
) -> None:
    _plugin, _client, api = plugin_env
    result = await api.tools.call(
        "obs.request",
        {
            "request_type": "CreateInput",
            "request_data": {"sceneName": "Live", "inputName": "X", "inputKind": "no_such_kind"},
        },
    )
    assert result["ok"] is False
    assert result["code"] == Status.INVALID_INPUT_KIND
    assert "no_such_kind" in result["comment"]


async def test_the_privacy_scene_cannot_be_changed_and_the_request_never_leaves(
    obs: FakeObs, plugin_env: tuple[ObsPlugin, FakeClient, Any]
) -> None:
    _plugin, _client, api = plugin_env
    with pytest.raises(PermissionError):
        await api.tools.call(
            "obs.request",
            {
                "request_type": "CreateInput",
                "request_data": {
                    "sceneName": "Technical Problems",
                    "inputName": "Intruder",
                    "inputKind": "browser_source",
                },
            },
        )
    assert obs.requests_of("CreateInput") == []
    assert "Intruder" not in obs.state.inputs


async def test_stopping_the_stream_is_refused_before_it_reaches_obs(
    obs: FakeObs, plugin_env: tuple[ObsPlugin, FakeClient, Any]
) -> None:
    _plugin, _client, api = plugin_env
    obs.state.streaming = True
    for request_type in ("StopStream", "ToggleStream"):
        with pytest.raises(PermissionError):
            await api.tools.call("obs.request", {"request_type": request_type})
    assert obs.requests_of("StopStream") == [] and obs.requests_of("ToggleStream") == []
    assert obs.state.streaming is True


async def test_the_stream_start_and_end_events_become_a_session(
    obs: FakeObs, plugin_env: tuple[ObsPlugin, FakeClient, Any]
) -> None:
    _plugin, client, api = plugin_env
    await api.tools.call("obs.request", {"request_type": "StartStream"})
    await wait_until(lambda: _events(client, "stream.started"), what="stream.started")
    # OBS reports STARTING and then STARTED: one session, not two.
    assert len(_events(client, "stream.started")) == 1
    session = _events(client, "stream.started")[0]["session_id"]
    assert (await api.tools.call("obs.status.read", {}))["streaming"] is True

    obs.state.streaming = False  # the streamer stops it in OBS
    await obs.emit("StreamStateChanged", {"outputActive": False, "outputState": "STOPPED"})
    await wait_until(lambda: _events(client, "stream.ended"), what="stream.ended")
    assert _events(client, "stream.ended")[0]["session_id"] == session


async def test_a_stream_that_is_already_live_when_the_plugin_connects_is_noticed() -> None:
    async with FakeObs(scenes=("Start",)) as obs:
        obs.state.streaming = True
        client = FakeClient(secret=None)
        plugin = create(make_api(client, port=obs.port))
        await plugin.start()
        try:
            await wait_until(lambda: _events(client, "stream.started"), what="stream.started")
        finally:
            await plugin.stop()


async def test_a_recording_change_is_announced(
    obs: FakeObs, plugin_env: tuple[ObsPlugin, FakeClient, Any]
) -> None:
    _plugin, client, api = plugin_env
    await api.tools.call("obs.request", {"request_type": "StartRecord"})
    await api.tools.call("obs.request", {"request_type": "StopRecord"})
    await wait_until(lambda: len(_events(client, "obs.recording_changed")) == 2, what="both")
    assert [e["recording"] for e in _events(client, "obs.recording_changed")] == [True, False]


async def test_the_replay_buffer_save_resolves_the_path_from_the_event(
    obs: FakeObs, plugin_env: tuple[ObsPlugin, FakeClient, Any]
) -> None:
    _plugin, _client, api = plugin_env
    result = await api.tools.call("obs.replay_buffer.save", {})
    assert result == {"ok": True, "file_path": "replays/example-replay-0001.mkv", "reason": ""}


async def test_a_replay_buffer_that_is_off_is_a_clear_result_not_a_crash(
    obs: FakeObs, plugin_env: tuple[ObsPlugin, FakeClient, Any]
) -> None:
    _plugin, _client, api = plugin_env
    obs.state.replay_buffer_active = False
    result = await api.tools.call("obs.replay_buffer.save", {})
    assert result["ok"] is False
    assert "not enabled" in result["reason"]
    assert obs.requests_of("SaveReplayBuffer") == []


async def test_the_overlay_is_a_transparent_browser_source_made_once_and_updated_after(
    obs: FakeObs, plugin_env: tuple[ObsPlugin, FakeClient, Any]
) -> None:
    _plugin, _client, api = plugin_env
    url = "http://127.0.0.1:9/pet/?token=example"
    first = await api.tools.call("obs.overlay.setup", {"url": url, "scene_name": "Live"})
    assert first == {"input_name": "Nox Overlay", "scene": "Live", "created": True, "added": True}
    overlay = obs.state.inputs["Nox Overlay"]
    assert overlay.kind == "browser_source"
    assert overlay.settings["url"] == url
    assert "rgba(0, 0, 0, 0)" in overlay.settings["css"]

    second = await api.tools.call(
        "obs.overlay.setup", {"url": url, "scene_name": "Live", "width": 800}
    )
    assert second["created"] is False
    assert overlay.settings["width"] == 800
    assert [i.source_name for i in obs.state.scenes["Live"]] == ["Nox Overlay"]  # not twice


async def test_the_overlay_is_never_put_into_the_privacy_scene(
    obs: FakeObs, plugin_env: tuple[ObsPlugin, FakeClient, Any]
) -> None:
    _plugin, _client, api = plugin_env
    with pytest.raises(PermissionError, match="privacy scene"):
        await api.tools.call(
            "obs.overlay.setup",
            {"url": "http://127.0.0.1:9/pet/", "scene_name": "Technical Problems"},
        )
    assert obs.requests_of("CreateInput") == []


async def test_the_voice_track_is_set_up_on_the_virtual_cable_and_nothing_else(
    obs: FakeObs, plugin_env: tuple[ObsPlugin, FakeClient, Any]
) -> None:
    _plugin, _client, api = plugin_env
    result = await api.tools.call(
        "obs.audio_track.setup", {"scene_name": "Live", "tracks": [2], "device_hint": "Virtual"}
    )
    assert result["ok"] is True
    assert result["device"] == "Virtual Cable"
    assert obs.state.inputs["Nox Voice"].settings == {"device_id": "cable-0002"}
    assert obs.state.audio_tracks["Nox Voice"] == {
        "1": False,
        "2": True,
        "3": False,
        "4": False,
        "5": False,
        "6": False,
    }


async def test_a_voice_track_with_no_matching_device_leaves_no_input_behind(
    obs: FakeObs, plugin_env: tuple[ObsPlugin, FakeClient, Any]
) -> None:
    _plugin, _client, api = plugin_env
    obs.state.audio_devices = [("Studio Microphone", "mic-0001")]
    result = await api.tools.call("obs.audio_track.setup", {"scene_name": "Live"})
    assert result["ok"] is False
    assert "Nox Voice" not in obs.state.inputs
    assert obs.requests_of("RemoveInput") == [{"inputName": "Nox Voice"}]


async def test_the_panic_event_switches_to_the_privacy_scene_and_freezes_changes(
    obs: FakeObs, plugin_env: tuple[ObsPlugin, FakeClient, Any]
) -> None:
    _plugin, client, api = plugin_env
    await client.fire("security.panic")
    assert obs.state.current_scene == "Technical Problems"
    with pytest.raises(PermissionError, match="panic button"):
        await api.tools.call(
            "obs.request",
            {
                "request_type": "CreateScene",
                "request_data": {"sceneName": "Another"},
            },
        )
    assert "Another" not in obs.state.scenes


async def test_losing_obs_is_reported_and_getting_it_back_is_too(
    obs: FakeObs, plugin_env: tuple[ObsPlugin, FakeClient, Any]
) -> None:
    plugin, client, api = plugin_env
    obs.refuse_connections(10_000)
    await obs.drop_connections()
    await wait_until(lambda: not plugin.client.connected, what="the plugin to notice")
    assert len(_events(client, "obs.disconnected")) == 1
    assert (await api.tools.call("obs.status.read", {}))["connected"] is False
    with pytest.raises(ConnectionError):
        await api.tools.call("obs.scenes.list", {})
    health, reason = await plugin.health()
    assert health is HealthStatus.UNAVAILABLE
    assert reason

    obs.refuse_connections(0)
    await wait_until(lambda: plugin.client.identified, what="the plugin to come back")
    assert len(_events(client, "obs.connected")) == 2
    assert (await plugin.health())[0] is HealthStatus.AVAILABLE


async def test_a_wrong_password_is_a_health_reason_and_one_event(
    obs_with_password: FakeObs,
) -> None:
    client = FakeClient(secret="wrong")
    plugin = create(make_api(client, port=obs_with_password.port))
    await plugin.start()
    try:
        await wait_until(lambda: _events(client, "obs.auth_failed"), what="obs.auth_failed")
        await wait_until(lambda: obs_with_password.connection_count >= 3, what="more attempts")
        assert len(_events(client, "obs.auth_failed")) == 1
        health, reason = await plugin.health()
        assert health is HealthStatus.UNAVAILABLE
        assert "rejected" in reason
    finally:
        await plugin.stop()


async def test_a_missing_password_secret_is_a_health_reason() -> None:
    async with FakeObs(password=PASSWORD) as obs:
        client = FakeClient(secret=None)
        plugin = create(make_api(client, port=obs.port))
        await plugin.start()
        try:
            await wait_until(lambda: plugin.client.auth_failed, what="the failed login")
            health, reason = await plugin.health()
            assert health is HealthStatus.UNAVAILABLE
            assert "no secret configured" in reason
            assert obs.identifies == []
        finally:
            await plugin.stop()

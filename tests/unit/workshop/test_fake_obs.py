"""The fake OBS against the obs-websocket v5 specification, spoken to by a raw client that shares no
code with Nox's. If these fail, the fake is wrong - and every contract test built on it with it."""

from __future__ import annotations

import asyncio
import base64
import hashlib

import pytest

from tests.unit.fakes import wait_until
from tests.workshop.fake_obs import FakeObs, Status, auth_string
from tests.workshop.fake_obs.protocol import (
    CLOSE_ALREADY_IDENTIFIED,
    CLOSE_AUTHENTICATION_FAILED,
    CLOSE_NOT_IDENTIFIED,
    CLOSE_UNSUPPORTED_RPC_VERSION,
    OP_IDENTIFY,
    OP_REQUEST,
    SUB_ALL,
    SUB_OUTPUTS,
    SUB_SCENES,
)
from tests.workshop.raw_clients import RawObsClient

pytestmark = pytest.mark.timeout(30)


# -- the handshake ------------------------------------------------------------------------------


async def test_hello_names_the_versions_and_asks_for_no_password_when_there_is_none(
    obs: FakeObs,
) -> None:
    async with RawObsClient(obs.url) as client:
        hello = await client.read_hello()
    assert hello["obsWebSocketVersion"] == "5.5.4"
    assert hello["rpcVersion"] == 1
    assert "authentication" not in hello


async def test_hello_carries_a_fresh_challenge_and_salt_when_a_password_is_set(
    obs_with_password: FakeObs,
) -> None:
    seen = []
    for _ in range(2):
        async with RawObsClient(obs_with_password.url) as client:
            seen.append((await client.read_hello())["authentication"])
    assert all(set(auth) == {"challenge", "salt"} for auth in seen)
    assert seen[0] != seen[1]


def test_the_authentication_string_is_the_documented_double_hash() -> None:
    """sha256 over (password + salt), base64; then sha256 over (that + challenge), base64."""
    first = base64.b64encode(hashlib.sha256(b"pw" + b"salt").digest()).decode()
    expected = base64.b64encode(hashlib.sha256(first.encode() + b"challenge").digest()).decode()
    assert auth_string("pw", "salt", "challenge") == expected


async def test_identify_with_the_right_password_is_identified(obs_with_password: FakeObs) -> None:
    async with RawObsClient(obs_with_password.url) as client:
        reply = await client.identify(password="correct-horse")
    assert reply == {"negotiatedRpcVersion": 1}
    assert [i.authenticated for i in obs_with_password.identifies] == [True]


@pytest.mark.parametrize("password", ["wrong", None])
async def test_identify_with_a_wrong_or_missing_password_closes_with_4009(
    obs_with_password: FakeObs, password: str | None
) -> None:
    async with RawObsClient(obs_with_password.url) as client:
        await client.read_hello()
        data: dict = {"rpcVersion": 1}
        if password is not None:
            data["authentication"] = "not-the-hash"
        await client.send(OP_IDENTIFY, data)
        assert await client.close_code() == CLOSE_AUTHENTICATION_FAILED
    assert obs_with_password.identifies == []


async def test_a_newer_rpc_version_than_the_server_knows_closes_with_4010(obs: FakeObs) -> None:
    async with RawObsClient(obs.url) as client:
        await client.read_hello()
        await client.send(OP_IDENTIFY, {"rpcVersion": 2})
        assert await client.close_code() == CLOSE_UNSUPPORTED_RPC_VERSION


async def test_a_request_before_identify_closes_with_4007(obs: FakeObs) -> None:
    async with RawObsClient(obs.url) as client:
        await client.read_hello()
        await client.send(OP_REQUEST, {"requestType": "GetVersion", "requestId": "x"})
        assert await client.close_code() == CLOSE_NOT_IDENTIFIED
    assert obs.requests == []


async def test_a_second_identify_closes_with_4008(obs: FakeObs) -> None:
    async with RawObsClient(obs.url) as client:
        await client.identify()
        await client.send(OP_IDENTIFY, {"rpcVersion": 1})
        assert await client.close_code() == CLOSE_ALREADY_IDENTIFIED


# -- requests and their statuses ----------------------------------------------------------------


async def test_a_request_is_answered_with_its_id_type_and_status(obs: FakeObs) -> None:
    async with RawObsClient(obs.url) as client:
        await client.identify()
        response = await client.request("GetVersion")
    assert response["requestType"] == "GetVersion"
    assert response["requestStatus"] == {"result": True, "code": Status.SUCCESS}
    assert "GetSceneList" in response["responseData"]["availableRequests"]


@pytest.mark.parametrize(
    ("request_type", "data", "code"),
    [
        ("NoSuchRequest", {}, Status.UNKNOWN_REQUEST_TYPE),
        ("SetCurrentProgramScene", {}, Status.MISSING_REQUEST_FIELD),
        ("SetCurrentProgramScene", {"sceneName": 5}, Status.INVALID_REQUEST_FIELD_TYPE),
        ("SetCurrentProgramScene", {"sceneName": ""}, Status.REQUEST_FIELD_EMPTY),
        ("SetCurrentProgramScene", {"sceneName": "Nowhere"}, Status.RESOURCE_NOT_FOUND),
        ("CreateScene", {"sceneName": "Start"}, Status.RESOURCE_ALREADY_EXISTS),
        ("GetInputSettings", {"inputName": "Nope"}, Status.RESOURCE_NOT_FOUND),
        (
            "CreateInput",
            {"sceneName": "Start", "inputName": "X", "inputKind": "no_such_kind"},
            Status.INVALID_INPUT_KIND,
        ),
        ("StopStream", {}, Status.OUTPUT_NOT_RUNNING),
        ("StopRecord", {}, Status.OUTPUT_NOT_RUNNING),
    ],
)
async def test_a_request_that_cannot_work_is_answered_with_the_documented_code(
    obs: FakeObs, request_type: str, data: dict, code: int
) -> None:
    async with RawObsClient(obs.url) as client:
        await client.identify()
        response = await client.request(request_type, data)
    assert response["requestStatus"]["result"] is False
    assert response["requestStatus"]["code"] == code
    assert response["requestStatus"]["comment"]


async def test_a_failed_request_changes_nothing_and_emits_nothing(obs: FakeObs) -> None:
    async with RawObsClient(obs.url) as client:
        await client.identify()
        await client.request("SetCurrentProgramScene", {"sceneName": "Nowhere"})
        marker = await client.request("GetCurrentProgramScene")  # nothing queued before this
    assert obs.state.current_scene == "Start"
    assert marker["responseData"]["sceneName"] == "Start"


# -- state and events ---------------------------------------------------------------------------


async def test_switching_the_scene_changes_the_state_and_emits_the_event(obs: FakeObs) -> None:
    async with RawObsClient(obs.url) as client:
        await client.identify()
        await client.request("SetCurrentProgramScene", {"sceneName": "Live"})
        (event,) = await client.events(1)
    assert obs.state.current_scene == "Live"
    assert event["eventType"] == "CurrentProgramSceneChanged"
    assert event["eventData"]["sceneName"] == "Live"
    assert event["eventIntent"] == SUB_SCENES


async def test_creating_a_source_puts_it_into_the_scene_and_emits_both_events(
    obs: FakeObs,
) -> None:
    async with RawObsClient(obs.url) as client:
        await client.identify()
        response = await client.request(
            "CreateInput",
            {
                "sceneName": "Live",
                "inputName": "Overlay",
                "inputKind": "browser_source",
                "inputSettings": {"url": "https://example.invalid/overlay", "width": 800},
            },
        )
        events = await client.events(2)
        listed = await client.request("GetSceneItemList", {"sceneName": "Live"})
        fetched = await client.request("GetInputSettings", {"inputName": "Overlay"})
    assert response["requestStatus"]["result"] is True
    assert [e["eventType"] for e in events] == ["InputCreated", "SceneItemCreated"]
    items = listed["responseData"]["sceneItems"]
    assert [(i["sourceName"], i["sceneItemEnabled"]) for i in items] == [("Overlay", True)]
    assert fetched["responseData"]["inputSettings"]["width"] == 800
    assert obs.state.inputs["Overlay"].kind == "browser_source"


async def test_set_input_settings_overlays_unless_told_to_replace(obs: FakeObs) -> None:
    async with RawObsClient(obs.url) as client:
        await client.identify(event_subscriptions=0)
        await client.request(
            "CreateInput",
            {
                "sceneName": "Live",
                "inputName": "Overlay",
                "inputKind": "browser_source",
                "inputSettings": {"width": 800, "height": 600},
            },
        )
        await client.request(
            "SetInputSettings", {"inputName": "Overlay", "inputSettings": {"width": 1280}}
        )
        assert obs.state.inputs["Overlay"].settings == {"width": 1280, "height": 600}
        await client.request(
            "SetInputSettings",
            {"inputName": "Overlay", "inputSettings": {"width": 640}, "overlay": False},
        )
    assert obs.state.inputs["Overlay"].settings == {"width": 640}


async def test_removing_a_source_takes_it_out_of_every_scene_and_drops_its_filters(
    obs: FakeObs,
) -> None:
    async with RawObsClient(obs.url) as client:
        await client.identify(event_subscriptions=0)
        await client.request(
            "CreateInput",
            {"sceneName": "Live", "inputName": "Mic", "inputKind": "wasapi_input_capture"},
        )
        await client.request(
            "CreateSourceFilter",
            {"sourceName": "Mic", "filterName": "Gain", "filterKind": "gain_filter"},
        )
        await client.request("RemoveInput", {"inputName": "Mic"})
    assert "Mic" not in obs.state.inputs
    assert obs.state.scenes["Live"] == []
    assert "Mic" not in obs.state.filters


async def test_a_filter_can_be_added_switched_off_and_removed(obs: FakeObs) -> None:
    async with RawObsClient(obs.url) as client:
        await client.identify(event_subscriptions=0)
        await client.request(
            "CreateInput",
            {"sceneName": "Live", "inputName": "Cam", "inputKind": "dshow_input"},
        )
        created = await client.request(
            "CreateSourceFilter",
            {
                "sourceName": "Cam",
                "filterName": "Delay",
                "filterKind": "render_delay",
                "filterSettings": {"delay_ms": 3000},
            },
        )
        again = await client.request(
            "CreateSourceFilter",
            {"sourceName": "Cam", "filterName": "Delay", "filterKind": "render_delay"},
        )
        bad_kind = await client.request(
            "CreateSourceFilter",
            {"sourceName": "Cam", "filterName": "Odd", "filterKind": "no_such_filter"},
        )
        await client.request(
            "SetSourceFilterEnabled",
            {"sourceName": "Cam", "filterName": "Delay", "filterEnabled": False},
        )
        listed = await client.request("GetSourceFilterList", {"sourceName": "Cam"})
        await client.request("RemoveSourceFilter", {"sourceName": "Cam", "filterName": "Delay"})
    assert created["requestStatus"]["result"] is True
    assert again["requestStatus"]["code"] == Status.RESOURCE_ALREADY_EXISTS
    assert bad_kind["requestStatus"]["code"] == Status.INVALID_FILTER_KIND
    (entry,) = listed["responseData"]["filters"]
    assert (entry["filterName"], entry["filterEnabled"]) == ("Delay", False)
    assert entry["filterSettings"] == {"delay_ms": 3000}
    assert obs.state.filters["Cam"] == []


async def test_going_live_and_stopping_follow_real_obs_states(obs: FakeObs) -> None:
    async with RawObsClient(obs.url) as client:
        await client.identify()
        await client.request("StartStream")
        started = await client.events(2)
        twice = await client.request("StartStream")
        status = await client.request("GetStreamStatus")
        await client.request("StopStream")
        stopped = await client.events(2)
    assert [e["eventData"]["outputState"] for e in started] == [
        "OBS_WEBSOCKET_OUTPUT_STARTING",
        "OBS_WEBSOCKET_OUTPUT_STARTED",
    ]
    assert twice["requestStatus"]["code"] == Status.OUTPUT_RUNNING
    assert status["responseData"]["outputActive"] is True
    assert [e["eventData"]["outputState"] for e in stopped] == [
        "OBS_WEBSOCKET_OUTPUT_STOPPING",
        "OBS_WEBSOCKET_OUTPUT_STOPPED",
    ]
    assert obs.state.streaming is False


async def test_recording_reports_its_file_when_it_stops(obs: FakeObs) -> None:
    async with RawObsClient(obs.url) as client:
        await client.identify()
        await client.request("StartRecord")
        await client.events(1)
        stopped = await client.request("StopRecord")
        (event,) = await client.events(1)
    assert stopped["responseData"]["outputPath"].endswith(".mkv")
    assert event["eventData"]["outputPath"] == stopped["responseData"]["outputPath"]
    assert event["eventData"]["outputActive"] is False


async def test_removing_the_current_scene_moves_to_another_and_the_last_scene_stays() -> None:
    async with FakeObs(scenes=("One", "Two")) as obs, RawObsClient(obs.url) as client:
        await client.identify(event_subscriptions=0)
        await client.request("RemoveScene", {"sceneName": "One"})
        assert obs.state.current_scene == "Two"
        last = await client.request("RemoveScene", {"sceneName": "Two"})
    assert last["requestStatus"]["code"] == Status.NOT_ENOUGH_RESOURCES
    assert obs.state.scene_names == ["Two"]


# -- event subscriptions ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subscriptions", "scene_events", "output_events"),
    [
        (SUB_ALL, 1, 2),
        (SUB_SCENES, 1, 0),
        (SUB_OUTPUTS, 0, 2),
        (0, 0, 0),
    ],
)
async def test_events_reach_only_clients_subscribed_to_their_category(
    obs: FakeObs, subscriptions: int, scene_events: int, output_events: int
) -> None:
    async with RawObsClient(obs.url) as watcher, RawObsClient(obs.url) as driver:
        await watcher.identify(event_subscriptions=subscriptions)
        await driver.identify(event_subscriptions=0)
        await driver.request("SetCurrentProgramScene", {"sceneName": "Live"})
        await driver.request("StartStream")
        await driver.request("GetVersion")  # the last thing the driver does
        marker = await watcher.request("GetVersion")  # answered after every event was sent
        assert marker["requestStatus"]["result"] is True
        await obs.emit("ExitStarted")  # General: only a SUB_ALL watcher gets it
        general = 1 if subscriptions == SUB_ALL else 0
        received = await watcher.events(scene_events + output_events + general)
    kinds = [e["eventType"] for e in received]
    assert kinds.count("CurrentProgramSceneChanged") == scene_events
    assert kinds.count("StreamStateChanged") == output_events
    assert kinds.count("ExitStarted") == general


async def test_without_an_event_subscription_field_a_client_gets_everything(obs: FakeObs) -> None:
    async with RawObsClient(obs.url) as client:
        await client.identify()
        await obs.emit("ExitStarted")
        (event,) = await client.events(1)
    assert event["eventType"] == "ExitStarted"
    assert obs.identifies[0].event_subscriptions == SUB_ALL


# -- the failures a test can script ---------------------------------------------------------------


async def test_fail_next_answers_the_next_requests_with_the_scripted_failure(obs: FakeObs) -> None:
    obs.fail_next("GetStreamStatus", Status.REQUEST_PROCESSING_FAILED, "boom", times=2)
    async with RawObsClient(obs.url) as client:
        await client.identify()
        first = await client.request("GetStreamStatus")
        second = await client.request("GetStreamStatus")
        third = await client.request("GetStreamStatus")
    assert [r["requestStatus"].get("comment") for r in (first, second)] == ["boom", "boom"]
    assert third["requestStatus"]["result"] is True


async def test_script_replaces_the_built_in_answer(obs: FakeObs) -> None:
    obs.script("GetStreamStatus", lambda data: {"outputActive": True, "seen": data})
    async with RawObsClient(obs.url) as client:
        await client.identify()
        response = await client.request("GetStreamStatus", {"x": 1})
    assert response["responseData"] == {"outputActive": True, "seen": {"x": 1}}


async def test_ignore_receives_the_request_but_never_answers_it(obs: FakeObs) -> None:
    obs.ignore("GetVersion")
    async with RawObsClient(obs.url) as client:
        await client.identify()
        await client.send(OP_REQUEST, {"requestType": "GetVersion", "requestId": "v"})
        await asyncio.wait_for(obs.arrived("GetVersion").wait(), 5)
        answered = await client.request("GetStats")  # a later request is still answered
    assert answered["requestStatus"]["result"] is True
    assert obs.requests_of("GetVersion") == [{}]


async def test_hold_answers_only_after_the_gate_is_opened(obs: FakeObs) -> None:
    gate = obs.hold("GetVersion")
    async with RawObsClient(obs.url) as client:
        await client.identify()
        await client.send(OP_REQUEST, {"requestType": "GetVersion", "requestId": "v"})
        await asyncio.wait_for(obs.arrived("GetVersion").wait(), 5)
        assert not gate.is_set()
        gate.set()
        reply = await client.receive()
    assert reply["d"]["requestId"] == "v"
    assert reply["d"]["requestStatus"]["result"] is True


async def test_drop_on_cuts_the_connection_when_the_request_arrives(obs: FakeObs) -> None:
    obs.drop_on("GetVersion")
    async with RawObsClient(obs.url) as client:
        await client.identify()
        await client.send(OP_REQUEST, {"requestType": "GetVersion", "requestId": "v"})
        await asyncio.wait_for(client.ws.wait_closed(), 5)
        assert client.ws.close_code == 1006
    assert obs.requests_of("GetVersion") == [{}]


async def test_drop_connections_cuts_every_client_without_a_close_handshake(obs: FakeObs) -> None:
    async with RawObsClient(obs.url) as one, RawObsClient(obs.url) as two:
        await one.identify()
        await two.identify()
        await wait_until(lambda: obs.identified_clients == 2, what="two identified clients")
        await obs.drop_connections()
        await asyncio.wait_for(one.ws.wait_closed(), 5)
        await asyncio.wait_for(two.ws.wait_closed(), 5)
        assert (one.ws.close_code, two.ws.close_code) == (1006, 1006)
    await wait_until(lambda: obs.identified_clients == 0, what="the server noticing")


async def test_close_connections_closes_properly_with_the_given_code(obs: FakeObs) -> None:
    async with RawObsClient(obs.url) as client:
        await client.identify()
        await obs.close_connections(1001, "OBS is closing")
        assert await client.close_code() == 1001


async def test_refuse_connections_closes_the_next_ones_before_hello_then_recovers(
    obs: FakeObs,
) -> None:
    obs.refuse_connections(2)
    for _ in range(2):
        async with RawObsClient(obs.url) as client:
            assert await client.close_code() == 1013
    async with RawObsClient(obs.url) as client:
        assert (await client.read_hello())["rpcVersion"] == 1
    assert obs.connection_count == 3


async def test_stall_hello_accepts_the_connection_and_says_nothing(obs: FakeObs) -> None:
    obs.stall_hello()
    async with RawObsClient(obs.url) as client:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(client.ws.recv(), 0.3)
        assert obs.connection_count == 1

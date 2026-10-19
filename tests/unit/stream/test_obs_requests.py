"""nox.stream.obs_requests: every obs-websocket request type gets an action and a risk."""

from __future__ import annotations

import pytest

from nox.security.model import Risk
from nox.security.prohibitions import hard_prohibition_for
from nox.stream.obs_requests import (
    BLOCKED,
    HARD_PROHIBITED,
    HIGH_TYPES,
    MEDIUM_TYPES,
    READ_TYPES,
    InvalidObsRequestError,
    classify,
    refusal,
    scope_for_tool,
)


def test_tables_do_not_overlap() -> None:
    tables = [set(HARD_PROHIBITED), set(BLOCKED), READ_TYPES, MEDIUM_TYPES, HIGH_TYPES]
    for i, left in enumerate(tables):
        for right in tables[i + 1 :]:
            assert not left & right, left & right


@pytest.mark.parametrize("request_type", sorted(READ_TYPES))
def test_read_types_are_read(request_type: str) -> None:
    scope = classify(request_type)
    assert scope.risk is Risk.READ
    assert scope.action == f"request.{request_type}"


def test_changes_are_medium_and_keep_their_type_in_the_action() -> None:
    scope = classify("CreateScene", {"sceneName": "Just Chatting"})
    assert scope.risk is Risk.MEDIUM
    # The type in the action is what keeps a remembered answer to this one request type.
    assert scope.action == "request.CreateScene"
    assert classify("SetSceneItemTransform").action == "request.SetSceneItemTransform"


@pytest.mark.parametrize(
    "request_type", ["StopStream", "ToggleStream", "StopOutput", "ToggleOutput"]
)
def test_stopping_the_stream_is_the_hard_prohibition(request_type: str) -> None:
    scope = classify(request_type)
    assert scope.action == "stream.stop"
    assert hard_prohibition_for("obs", scope.action) == "stream.stop"


def test_reading_the_stream_key_is_the_hard_prohibition() -> None:
    scope = classify("GetStreamServiceSettings")
    assert hard_prohibition_for("obs", scope.action) == "stream.key.read"


@pytest.mark.parametrize("request_type", sorted(BLOCKED))
def test_blocked_types_are_critical(request_type: str) -> None:
    assert classify(request_type).risk is Risk.CRITICAL
    assert refusal(request_type) is not None


def test_destructive_types_are_high_with_the_profile_action_names() -> None:
    assert classify("RemoveScene").action == "scene.delete"
    assert classify("SetSceneName").action == "scene.rename"
    assert classify("RemoveInput").action == "source.delete"
    for request_type in ("RemoveScene", "RemoveInput", "StartStream", "RemoveSceneItem"):
        assert classify(request_type).risk is Risk.HIGH


def test_unknown_types_are_not_trusted() -> None:
    assert classify("GetSomethingNew").risk is Risk.MEDIUM
    assert classify("DeleteEverything").risk is Risk.HIGH


@pytest.mark.parametrize("bad", ["", "getSceneList", "Get Scene", "Get.Scene", "Ünicode"])
def test_malformed_type_names_are_refused(bad: str) -> None:
    with pytest.raises(InvalidObsRequestError):
        classify(bad)


def test_privacy_scene_cannot_be_changed_but_can_be_switched_to() -> None:
    privacy = "Technical Problems"
    for request_type, data in [
        ("RemoveScene", {"sceneName": privacy}),
        ("SetSceneName", {"sceneName": "Live", "newSceneName": privacy}),
        ("SetSceneItemEnabled", {"sceneName": privacy, "sceneItemId": 1}),
        ("CreateInput", {"sceneName": privacy, "inputName": "x", "inputKind": "image_source"}),
    ]:
        scope = classify(request_type, data, privacy_scene=privacy)
        assert scope.risk is Risk.CRITICAL, request_type
    switch = classify("SetCurrentProgramScene", {"sceneName": privacy}, privacy_scene=privacy)
    assert switch.risk is Risk.MEDIUM
    reading = classify("GetSceneItemList", {"sceneName": privacy}, privacy_scene=privacy)
    assert reading.risk is Risk.READ


def test_tool_scope_reads_the_plugin_config() -> None:
    scope = scope_for_tool({"privacy_scene": "Pause"})
    removed = scope({"request_type": "RemoveScene", "request_data": {"sceneName": "Pause"}})
    assert removed.risk is Risk.CRITICAL
    other = scope({"request_type": "RemoveScene", "request_data": {"sceneName": "Live"}})
    assert other.risk is Risk.HIGH

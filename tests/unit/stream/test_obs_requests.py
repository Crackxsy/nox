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
    privacy_refusal,
    redact,
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
    # Fail closed: an unlisted type is high (asked every time) whatever its name suggests.
    assert classify("GetSomethingNew").risk is Risk.HIGH
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


# ---- review fixes: one class per effect, fail closed, privacy scene by UUID, redaction -------

#: Every request type of the obs-websocket 5.x protocol (protocol.md, "Requests").
PROTOCOL_REQUEST_TYPES: frozenset[str] = frozenset(
    {
        # General
        "GetVersion", "GetStats", "BroadcastCustomEvent", "CallVendorRequest", "GetHotkeyList",
        "TriggerHotkeyByName", "TriggerHotkeyByKeySequence", "Sleep",
        # Config
        "GetPersistentData", "SetPersistentData", "GetSceneCollectionList",
        "SetCurrentSceneCollection", "CreateSceneCollection", "GetProfileList",
        "SetCurrentProfile", "CreateProfile", "RemoveProfile", "GetProfileParameter",
        "SetProfileParameter", "GetVideoSettings", "SetVideoSettings",
        "GetStreamServiceSettings", "SetStreamServiceSettings", "GetRecordDirectory",
        "SetRecordDirectory",
        # Sources
        "GetSourceActive", "GetSourceScreenshot", "SaveSourceScreenshot",
        # Scenes
        "GetSceneList", "GetGroupList", "GetCurrentProgramScene", "SetCurrentProgramScene",
        "GetCurrentPreviewScene", "SetCurrentPreviewScene", "CreateScene", "RemoveScene",
        "SetSceneName", "GetSceneSceneTransitionOverride", "SetSceneSceneTransitionOverride",
        # Inputs
        "GetInputList", "GetInputKindList", "GetSpecialInputs", "CreateInput", "RemoveInput",
        "SetInputName", "GetInputDefaultSettings", "GetInputSettings", "SetInputSettings",
        "GetInputMute", "SetInputMute", "ToggleInputMute", "GetInputVolume", "SetInputVolume",
        "GetInputAudioBalance", "SetInputAudioBalance", "GetInputAudioSyncOffset",
        "SetInputAudioSyncOffset", "GetInputAudioMonitorType", "SetInputAudioMonitorType",
        "GetInputAudioTracks", "SetInputAudioTracks", "GetInputDeinterlaceMode",
        "SetInputDeinterlaceMode", "GetInputDeinterlaceFieldOrder",
        "SetInputDeinterlaceFieldOrder", "GetInputPropertiesListPropertyItems",
        "PressInputPropertiesButton",
        # Transitions
        "GetTransitionKindList", "GetSceneTransitionList", "GetCurrentSceneTransition",
        "SetCurrentSceneTransition", "SetCurrentSceneTransitionDuration",
        "SetCurrentSceneTransitionSettings", "GetCurrentSceneTransitionCursor",
        "TriggerStudioModeTransition", "SetTBarPosition",
        # Filters
        "GetSourceFilterKindList", "GetSourceFilterList", "GetSourceFilterDefaultSettings",
        "CreateSourceFilter", "RemoveSourceFilter", "SetSourceFilterName", "GetSourceFilter",
        "SetSourceFilterIndex", "SetSourceFilterSettings", "SetSourceFilterEnabled",
        # Scene items
        "GetSceneItemList", "GetGroupSceneItemList", "GetSceneItemId", "GetSceneItemSource",
        "CreateSceneItem", "RemoveSceneItem", "DuplicateSceneItem", "GetSceneItemTransform",
        "SetSceneItemTransform", "GetSceneItemEnabled", "SetSceneItemEnabled",
        "GetSceneItemLocked", "SetSceneItemLocked", "GetSceneItemIndex", "SetSceneItemIndex",
        "GetSceneItemBlendMode", "SetSceneItemBlendMode",
        # Outputs
        "GetVirtualCamStatus", "ToggleVirtualCam", "StartVirtualCam", "StopVirtualCam",
        "GetReplayBufferStatus", "ToggleReplayBuffer", "StartReplayBuffer", "StopReplayBuffer",
        "SaveReplayBuffer", "GetLastReplayBufferReplay", "GetOutputList", "GetOutputStatus",
        "ToggleOutput", "StartOutput", "StopOutput", "GetOutputSettings", "SetOutputSettings",
        # Stream
        "GetStreamStatus", "ToggleStream", "StartStream", "StopStream", "SendStreamCaption",
        # Record
        "GetRecordStatus", "ToggleRecord", "StartRecord", "StopRecord", "ToggleRecordPause",
        "PauseRecord", "ResumeRecord", "SplitRecordFile", "CreateRecordChapter",
        # Media inputs
        "GetMediaInputStatus", "SetMediaInputCursor", "OffsetMediaInputCursor",
        "TriggerMediaInputAction",
        # UI
        "GetStudioModeEnabled", "SetStudioModeEnabled", "OpenInputPropertiesDialog",
        "OpenInputFiltersDialog", "OpenInputInteractDialog", "GetMonitorList",
        "OpenVideoMixProjector", "OpenSourceProjector",
    }
)  # fmt: skip


def test_every_protocol_request_type_is_classified_explicitly() -> None:
    listed = set(HARD_PROHIBITED) | set(BLOCKED) | READ_TYPES | MEDIUM_TYPES | HIGH_TYPES
    assert sorted(PROTOCOL_REQUEST_TYPES - listed) == []


@pytest.mark.parametrize(
    "request_type",
    sorted(PROTOCOL_REQUEST_TYPES | {"GetNewThing", "ListThings", "SetNewThing", "StartThing"}),
)
def test_nothing_is_read_or_medium_unless_listed_by_name(request_type: str) -> None:
    scope = classify(request_type)
    if scope.risk is Risk.READ:
        assert request_type in READ_TYPES
    if scope.risk is Risk.MEDIUM:
        assert request_type in MEDIUM_TYPES


@pytest.mark.parametrize(
    "request_type",
    [
        "StartOutput",  # the generic route to going live
        "StartStream",
        "StartRecord",
        "ToggleRecord",
        "ResumeRecord",
        "ToggleRecordPause",  # resumes a paused recording, like ResumeRecord
        "StartVirtualCam",
        "ToggleVirtualCam",
        "SendStreamCaption",
        "BroadcastCustomEvent",
        "PressInputPropertiesButton",
        "SetCurrentProfile",
        "CreateProfile",
        "RemoveProfile",
        "SetCurrentSceneCollection",
        "CreateSceneCollection",
    ],
)
def test_requests_with_the_same_effect_as_a_high_one_are_high(request_type: str) -> None:
    assert classify(request_type).risk is Risk.HIGH


def test_profile_parameters_cannot_be_set() -> None:
    # It reaches the recording path and output settings, which are blocked on their own.
    assert classify("SetProfileParameter").risk is Risk.CRITICAL


def test_reads_that_can_return_secrets_are_confirmed() -> None:
    assert classify("GetProfileParameter").risk is Risk.MEDIUM
    assert classify("GetPersistentData").risk is Risk.MEDIUM


@pytest.mark.parametrize(
    ("request_type", "data"),
    [
        ("CreateInput", {"inputKind": "image_source", "inputSettings": {"file": "C:/x.png"}}),
        ("SetInputSettings", {"inputName": "Web", "inputSettings": {"url": "https://x.example"}}),
        ("SetInputSettings", {"inputName": "Media", "inputSettings": {"local_file": "D:/v.mp4"}}),
        ("SetInputSettings", {"inputName": "Text", "inputSettings": {"text": "/etc/passwd"}}),
        ("CreateSourceFilter", {"filterSettings": {"image_path": "C:/mask.png"}}),
    ],
)
def test_settings_that_point_at_content_are_high(request_type: str, data: dict) -> None:
    assert classify(request_type, data).risk is Risk.HIGH


def test_plain_settings_stay_medium() -> None:
    data = {"inputName": "Title", "inputSettings": {"text": "Back soon", "font": {"size": 48}}}
    assert classify("SetInputSettings", data).risk is Risk.MEDIUM


def test_uuids_make_a_change_high_until_the_plugin_resolved_them() -> None:
    scope = classify("SetSceneItemEnabled", {"sceneUuid": "abc", "sceneItemId": 3})
    assert scope.risk is Risk.HIGH
    assert classify("SetInputMute", {"inputUuid": "def", "inputMuted": True}).risk is Risk.HIGH


def test_confirmation_text_names_the_target_without_secrets() -> None:
    scope = classify(
        "SetInputSettings",
        {"inputName": "Nox Overlay", "inputSettings": {"url": "http://127.0.0.1/pet/#overlay=s3"}},
    )
    assert scope.target.startswith("SetInputSettings inputName=Nox Overlay")
    assert "s3" not in scope.target and "url" in scope.target


class TestPrivacyRefusal:
    SCENE = {"scene_name": "Technical Problems", "scene_uuid": "uuid-privacy"}

    def refuse(self, request_type: str, data: dict) -> str | None:
        return privacy_refusal(
            request_type,
            data,
            **self.SCENE,
            source_names=["BRB Image"],
            source_uuids=["uuid-brb"],
        )

    def test_scene_by_uuid(self) -> None:
        assert self.refuse("RemoveScene", {"sceneUuid": "uuid-privacy"})
        assert self.refuse("SetSceneItemEnabled", {"sceneUuid": "uuid-privacy", "sceneItemId": 1})

    def test_sources_inside_it_by_name_and_uuid(self) -> None:
        assert self.refuse("SetInputSettings", {"inputName": "BRB Image", "inputSettings": {}})
        assert self.refuse("RemoveInput", {"inputUuid": "uuid-brb"})
        assert self.refuse("SetInputName", {"inputName": "Cam", "newInputName": "BRB Image"})

    def test_switching_to_it_and_unrelated_changes_pass(self) -> None:
        assert self.refuse("SetCurrentProgramScene", {"sceneUuid": "uuid-privacy"}) is None
        assert self.refuse("SetInputMute", {"inputName": "Mic", "inputMuted": True}) is None
        assert self.refuse("GetInputSettings", {"inputName": "BRB Image"}) is None


def test_redaction_of_obs_answers() -> None:
    answer = {
        "inputSettings": {
            "url": "http://127.0.0.1:47801/pet/?overlay=1&token=abc#overlay=SECRET",
            "server": "rtmp://live.example.tv/app/live_123_KEY",
            "password": "hunter2",
            "stream_key": "live_123",
            "text": "visit https://user:pw@example.com/x?sig=zzz&page=2",
            "width": 400,
        },
        "parameterValue": "plain",
    }
    redacted = redact(answer)
    flat = str(redacted)
    for secret in ("SECRET", "abc", "live_123", "hunter2", "pw@", "zzz", "KEY"):
        assert secret not in flat, secret
    assert redacted["inputSettings"]["width"] == 400
    assert "page=2" in redacted["inputSettings"]["text"]
    assert redacted["parameterValue"] == "plain"

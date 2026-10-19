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
        ("CreateInput", {"inputKind": "browser_source", "inputSettings": {}}),
        ("CreateInput", {"inputKind": "monitor_capture", "inputSettings": {}}),
        ("SetInputSettings", {"inputName": "Web", "inputSettings": {"url": "https://x.example"}}),
        ("SetInputSettings", {"inputName": "Media", "inputSettings": {"local_file": "D:/v.mp4"}}),
        ("SetInputSettings", {"inputName": "Text", "inputSettings": {"text": "/etc/passwd"}}),
        # Review payload: uppercase drive letter with a backslash.
        ("SetInputSettings", {"inputName": "Text", "inputSettings": {"text": "C:\\Users\\x"}}),
        # Any scheme, any case.
        ("SetInputSettings", {"inputName": "Text", "inputSettings": {"text": "SMB:share"}}),
        # A key nobody listed is unknown, so high - whatever it is called.
        ("SetInputSettings", {"inputName": "X", "inputSettings": {"stream_passphrase": "a"}}),
        ("CreateSourceFilter", {"filterKind": "mask_filter_v2", "filterSettings": {}}),
        ("CreateSourceFilter", {"filterKind": "crop_filter", "filterSettings": {"x": "C:/a"}}),
        ("SetSourceFilterSettings", {"filterSettings": {"image_path": "mask.png"}}),
        ("SetCurrentSceneTransitionSettings", {"transitionSettings": {"path": "stinger.webm"}}),
        ("SetSceneItemEnabled", {"sceneName": "Live", "sceneItemId": 3, "sceneItemEnabled": True}),
    ],
)
def test_anything_beyond_the_listed_harmless_settings_is_high(
    request_type: str, data: dict
) -> None:
    assert classify(request_type, data).risk is Risk.HIGH


@pytest.mark.parametrize(
    ("request_type", "data"),
    [
        (
            "SetInputSettings",
            {"inputName": "Title", "inputSettings": {"text": "Back soon", "font": {"size": 48}}},
        ),
        (
            "CreateInput",
            {
                "sceneName": "Live",
                "inputName": "Title",
                "inputKind": "text_gdiplus_v3",
                "inputSettings": {"text": "Hi", "color": 4294967295},
            },
        ),
        ("CreateSourceFilter", {"filterKind": "crop_filter", "filterSettings": {"left": 10}}),
        ("SetSceneItemEnabled", {"sceneName": "Live", "sceneItemId": 3, "sceneItemEnabled": False}),
    ],
)
def test_listed_harmless_settings_stay_medium(request_type: str, data: dict) -> None:
    assert classify(request_type, data).risk is Risk.MEDIUM


def test_adding_an_existing_source_to_a_scene_is_high() -> None:
    assert classify("CreateSceneItem", {"sceneName": "A", "sourceName": "Cam"}).risk is Risk.HIGH


def test_uuids_make_a_change_high_until_the_plugin_resolved_them() -> None:
    scope = classify("SetSceneItemLocked", {"sceneUuid": "abc", "sceneItemId": 3})
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
    OWN = {"Technical Problems", "uuid-privacy"}
    # The scene's contents as the plugin collects them: an image, a nested group and what is
    # inside that group.
    PROTECTED = {"BRB Image", "uuid-brb", "Overlay Group", "uuid-group", "Group Text", "uuid-gt"}

    def refuse(self, request_type: str, data: dict) -> str | None:
        return privacy_refusal(request_type, data, scene_names=self.OWN, protected=self.PROTECTED)

    def test_scene_by_name_and_uuid(self) -> None:
        assert self.refuse("RemoveScene", {"sceneUuid": "uuid-privacy"})
        assert self.refuse("SetSceneItemEnabled", {"sceneUuid": "uuid-privacy", "sceneItemId": 1})
        assert self.refuse(
            "SetSceneName", {"sceneName": "Live", "newSceneName": "Technical Problems"}
        )

    def test_sources_inside_it_by_name_and_uuid(self) -> None:
        assert self.refuse("SetInputSettings", {"inputName": "BRB Image", "inputSettings": {}})
        assert self.refuse("RemoveInput", {"inputUuid": "uuid-brb"})
        assert self.refuse("SetInputName", {"inputName": "Cam", "newInputName": "BRB Image"})
        assert self.refuse("CreateSourceFilter", {"sourceName": "BRB Image", "filterName": "f"})

    def test_an_input_inside_a_nested_group(self) -> None:
        # Review payload: the input sits in a group inside the privacy scene.
        assert self.refuse("SetInputSettings", {"inputName": "Group Text", "inputSettings": {}})
        assert self.refuse("SetSceneItemEnabled", {"sceneName": "Overlay Group", "sceneItemId": 2})
        assert self.refuse("RemoveInput", {"inputUuid": "uuid-gt"})

    def test_switching_to_it_and_unrelated_changes_pass(self) -> None:
        assert self.refuse("SetCurrentProgramScene", {"sceneUuid": "uuid-privacy"}) is None
        assert self.refuse("SetCurrentProgramScene", {"sceneName": "Technical Problems"}) is None
        assert self.refuse("SetInputMute", {"inputName": "Mic", "inputMuted": True}) is None
        assert self.refuse("GetInputSettings", {"inputName": "BRB Image"}) is None

    def test_switching_does_not_unlock_its_contents(self) -> None:
        assert self.refuse("SetCurrentProgramScene", {"sceneName": "Overlay Group"})


def test_redaction_keeps_only_listed_settings() -> None:
    answer = {
        "inputKind": "browser_source",
        "inputSettings": {
            "url": "http://127.0.0.1:47801/pet/?overlay=1&token=abc#overlay=SECRET",
            "stream_passphrase": "correct horse",  # review payload: secret word inside the key
            "css": "body { background: url(https://x.example/a.png) }",
            "width": 400,
            "text": "hello",
        },
    }
    redacted = redact(answer)
    settings = redacted["inputSettings"]
    assert settings["url"] == "http://127.0.0.1:47801"
    assert settings["stream_passphrase"] == "[redacted]"
    assert settings["css"] == "[redacted]"  # not on the list
    assert settings["text"] == "hello" and settings["width"] == 400
    assert "SECRET" not in str(redacted) and "abc" not in str(redacted)
    assert "correct horse" not in str(redacted)


def test_redaction_of_tokens_in_url_paths_and_key_like_strings() -> None:
    answer = {
        # Review payload: the token is in the URL path, not in a parameter.
        "comment": "failed to open https://api.example.com/hooks/T0K3Nabcdefghijklmnop/send",
        "savedReplayPath": "D:/Videos/Replay 2026-10-19.mkv",
        "sceneUuid": "1b4e28ba-2fa1-11d2-883f-0016d3cca427",
        "outputName": "live_123456789_AbCdEfGhIjKlMnOp",
        "authToken": "x",
        "monitorKeyFile": "y",
    }
    redacted = redact(answer)
    assert redacted["comment"] == "failed to open https://api.example.com"
    assert redacted["sceneUuid"] == answer["sceneUuid"]
    assert "AbCdEfGhIjKlMnOp" not in redacted["outputName"]
    assert redacted["authToken"] == "[redacted]" and redacted["monitorKeyFile"] == "[redacted]"
    assert redacted["savedReplayPath"].startswith("D:/Videos/Replay")


def test_file_settings_show_only_the_file_name() -> None:
    redacted = redact({"inputSettings": {"file": "C:\\Users\\someone\\Pictures\\brb.png"}})
    assert redacted["inputSettings"]["file"] == "brb.png"

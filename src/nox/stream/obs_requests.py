"""What a generic `obs.request` call may do, and how much it risks.

`obs.request` forwards one obs-websocket v5 request of any type to OBS. Its risk is not a property
of the tool but of the request type, so this module classifies every call into a `CallScope`
before the permission engine sees it (`nox.tools.registry.CallScope`):

* **read** - `Get*`/`List*` types obs-websocket documents as read-only. Allowed without asking.
* **medium** - changes that are easy to undo: creating scenes and sources, moving and resizing
  them, filters, audio levels, transitions, switching scenes. Confirmed; "remember for this
  session" applies to that one request type, because the permission action is
  `request.<RequestType>`.
* **high** - hard to undo or very visible: deleting or renaming scenes and sources, starting the
  broadcast or a recording, switching profile or scene collection, opening windows on the
  streamer's desktop. Confirmed every time, never remembered (`NEVER_REMEMBERED`).
* **denied** - routed to an existing hard prohibition (`stream.stop`, `stream.key.read`), so no
  profile, grant or confirmation can ever allow them; or classified critical, which no tool call
  can reach either (redirecting the stream to another server, opaque vendor requests and hotkeys
  that can trigger anything, writing files to arbitrary paths).

The privacy scene - where the panic button sends the stream - cannot be deleted, renamed or edited
through this tool; switching to it stays possible.

Unknown request types are not trusted: an unknown `Get*` is confirmed like a change, anything else
unknown is high risk. A future obs-websocket version may add a destructive request this table has
never heard of.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from nox.security.model import Risk
from nox.tools.registry import CallScope, Scope

#: Hard prohibitions (`nox.security.hardlist`) a request type is routed to. The permission engine
#: matches the action itself against the immutable deny list.
HARD_PROHIBITED: Mapping[str, str] = {
    # Stopping the broadcast is a hard prohibition: the streamer stops it, never Nox. The
    # Toggle and generic output verbs can stop it just as well.
    "StopStream": "stream.stop",
    "ToggleStream": "stream.stop",
    "StopOutput": "stream.stop",
    "ToggleOutput": "stream.stop",
    # Returns the stream key.
    "GetStreamServiceSettings": "stream.key.read",
}

#: Never reachable through `obs.request`, whatever anyone confirms (classified critical).
BLOCKED: Mapping[str, str] = {
    "SetStreamServiceSettings": "would redirect the broadcast or replace the stream key",
    "GetOutputSettings": "output settings can carry stream credentials",
    "SetOutputSettings": "output settings can redirect the broadcast",
    "CallVendorRequest": "vendor requests are opaque third-party calls",
    "TriggerHotkeyByName": "a hotkey can trigger anything, stopping the stream included",
    "TriggerHotkeyByKeySequence": "a hotkey can trigger anything, stopping the stream included",
    "SaveSourceScreenshot": "writes a file to an arbitrary path; use obs.screenshot",
    "GetSourceScreenshot": "use obs.screenshot, which keeps the image local and small",
    "SetRecordDirectory": "changes where recordings are written on disk",
    "Sleep": "only meaningful inside request batches, which obs.request does not send",
}

#: Read-only request types (obs-websocket 5.x protocol), allowed without confirmation.
READ_TYPES: frozenset[str] = frozenset(
    {
        "GetVersion",
        "GetStats",
        "GetHotkeyList",
        "GetPersistentData",
        "GetSceneCollectionList",
        "GetProfileList",
        "GetProfileParameter",
        "GetVideoSettings",
        "GetRecordDirectory",
        "GetSourceActive",
        "GetSceneList",
        "GetGroupList",
        "GetCurrentProgramScene",
        "GetCurrentPreviewScene",
        "GetSceneSceneTransitionOverride",
        "GetInputList",
        "GetInputKindList",
        "GetSpecialInputs",
        "GetInputDefaultSettings",
        "GetInputSettings",
        "GetInputMute",
        "GetInputVolume",
        "GetInputAudioBalance",
        "GetInputAudioSyncOffset",
        "GetInputAudioMonitorType",
        "GetInputAudioTracks",
        "GetInputDeinterlaceMode",
        "GetInputDeinterlaceFieldOrder",
        "GetInputPropertiesListPropertyItems",
        "GetTransitionKindList",
        "GetSceneTransitionList",
        "GetCurrentSceneTransition",
        "GetCurrentSceneTransitionCursor",
        "GetSourceFilterKindList",
        "GetSourceFilterList",
        "GetSourceFilterDefaultSettings",
        "GetSourceFilter",
        "GetSceneItemList",
        "GetGroupSceneItemList",
        "GetSceneItemId",
        "GetSceneItemSource",
        "GetSceneItemTransform",
        "GetSceneItemEnabled",
        "GetSceneItemLocked",
        "GetSceneItemIndex",
        "GetSceneItemBlendMode",
        "GetVirtualCamStatus",
        "GetReplayBufferStatus",
        "GetLastReplayBufferReplay",
        "GetOutputList",
        "GetOutputStatus",
        "GetStreamStatus",
        "GetRecordStatus",
        "GetMediaInputStatus",
        "GetStudioModeEnabled",
        "GetMonitorList",
    }
)

#: Changes that are easy to undo: confirmed, and a "remember" covers that one request type.
MEDIUM_TYPES: frozenset[str] = frozenset(
    {
        "BroadcastCustomEvent",
        "SetCurrentProgramScene",
        "SetCurrentPreviewScene",
        "CreateScene",
        "SetSceneSceneTransitionOverride",
        "CreateInput",
        "SetInputName",
        "SetInputSettings",
        "SetInputMute",
        "ToggleInputMute",
        "SetInputVolume",
        "SetInputAudioBalance",
        "SetInputAudioSyncOffset",
        "SetInputAudioMonitorType",
        "SetInputAudioTracks",
        "SetInputDeinterlaceMode",
        "SetInputDeinterlaceFieldOrder",
        "PressInputPropertiesButton",
        "SetCurrentSceneTransition",
        "SetCurrentSceneTransitionDuration",
        "SetCurrentSceneTransitionSettings",
        "TriggerStudioModeTransition",
        "SetTBarPosition",
        "CreateSourceFilter",
        "SetSourceFilterName",
        "SetSourceFilterIndex",
        "SetSourceFilterSettings",
        "SetSourceFilterEnabled",
        "CreateSceneItem",
        "DuplicateSceneItem",
        "SetSceneItemTransform",
        "SetSceneItemEnabled",
        "SetSceneItemLocked",
        "SetSceneItemIndex",
        "SetSceneItemBlendMode",
        "ToggleVirtualCam",
        "StartVirtualCam",
        "StopVirtualCam",
        "ToggleReplayBuffer",
        "StartReplayBuffer",
        "StopReplayBuffer",
        "SaveReplayBuffer",
        "PauseRecord",
        "ToggleRecordPause",
        "SplitRecordFile",
        "CreateRecordChapter",
        "SendStreamCaption",
        "SetMediaInputCursor",
        "OffsetMediaInputCursor",
        "TriggerMediaInputAction",
        "SetStudioModeEnabled",
        "StartOutput",
    }
)

#: Hard to undo or very visible: confirmed every time, never remembered. A few carry the action
#: names the shipped profiles already have rules for, so a profile that forbids deleting scenes
#: keeps forbidding it here too.
HIGH_ACTIONS: Mapping[str, str] = {
    "RemoveScene": "scene.delete",
    "SetSceneName": "scene.rename",
    "RemoveInput": "source.delete",
}
HIGH_TYPES: frozenset[str] = frozenset(
    {
        *HIGH_ACTIONS,
        "RemoveSceneItem",
        "RemoveSourceFilter",
        "StartStream",
        "StartRecord",
        "StopRecord",
        "ToggleRecord",
        "ResumeRecord",
        "SetCurrentSceneCollection",
        "CreateSceneCollection",
        "SetCurrentProfile",
        "CreateProfile",
        "RemoveProfile",
        "SetProfileParameter",
        "SetVideoSettings",
        "SetPersistentData",
        "OpenInputPropertiesDialog",
        "OpenInputFiltersDialog",
        "OpenInputInteractDialog",
        "OpenVideoMixProjector",
        "OpenSourceProjector",
    }
)

#: Request types that may target the privacy scene without being a change *to* it.
_PRIVACY_SCENE_SAFE: frozenset[str] = frozenset(
    {"SetCurrentProgramScene", "SetCurrentPreviewScene", "CreateScene"}
)
#: `requestData` fields that name a scene.
_SCENE_FIELDS: tuple[str, ...] = ("sceneName", "newSceneName", "destinationSceneName")

#: Action prefix of every `obs.request` call; the request type follows it.
ACTION_PREFIX = "request."


class InvalidObsRequestError(ValueError):
    """A request this module refuses to classify at all (malformed type name)."""


def _touches_privacy_scene(request_data: Mapping[str, Any], privacy_scene: str) -> bool:
    if not privacy_scene:
        return False
    return any(request_data.get(name) == privacy_scene for name in _SCENE_FIELDS)


def classify(
    request_type: str,
    request_data: Mapping[str, Any] | None = None,
    *,
    privacy_scene: str = "",
) -> CallScope:
    """The permission action and risk of one `obs.request` call."""
    if not request_type.isascii() or not request_type.isalnum() or not request_type[:1].isupper():
        raise InvalidObsRequestError(f"not an obs-websocket request type: {request_type!r}")
    data = request_data or {}
    hard = HARD_PROHIBITED.get(request_type)
    if hard is not None:
        return CallScope(action=hard, risk=Risk.HIGH)
    if request_type in BLOCKED:
        return CallScope(action=f"{ACTION_PREFIX}{request_type}", risk=Risk.CRITICAL)
    if request_type in READ_TYPES:
        return CallScope(action=f"{ACTION_PREFIX}{request_type}", risk=Risk.READ)
    if request_type not in _PRIVACY_SCENE_SAFE and _touches_privacy_scene(data, privacy_scene):
        # The panic path relies on this scene being there, unchanged.
        return CallScope(action="privacy_scene.modify", risk=Risk.CRITICAL)
    if request_type in HIGH_TYPES:
        action = HIGH_ACTIONS.get(request_type, f"{ACTION_PREFIX}{request_type}")
        return CallScope(action=action, risk=Risk.HIGH)
    if request_type in MEDIUM_TYPES:
        return CallScope(action=f"{ACTION_PREFIX}{request_type}", risk=Risk.MEDIUM)
    if request_type.startswith(("Get", "List")):
        return CallScope(action=f"{ACTION_PREFIX}{request_type}", risk=Risk.MEDIUM)
    return CallScope(action=f"{ACTION_PREFIX}{request_type}", risk=Risk.HIGH)


def refusal(request_type: str) -> str | None:
    """Why `obs.request` never sends this type, or None when it may (after permission)."""
    if request_type in HARD_PROHIBITED:
        return f"{request_type} is a hard prohibition ({HARD_PROHIBITED[request_type]})"
    reason = BLOCKED.get(request_type)
    return f"{request_type} is not available: {reason}" if reason else None


def scope_for_tool(config: Mapping[str, Any]) -> Scope:
    """The `ToolSpec.scope` of `obs.request` for a plugin configured with `config`."""
    privacy_scene = str(config.get("privacy_scene", "") or "")

    def scope(payload: dict[str, Any]) -> CallScope:
        data = payload.get("request_data")
        return classify(
            str(payload.get("request_type", "")),
            data if isinstance(data, Mapping) else None,
            privacy_scene=privacy_scene,
        )

    return scope


#: Shown to the model as the tool description: enough to build a scene layout without guessing.
TOOL_GUIDE = """\
Send one obs-websocket v5 request to OBS. Input: request_type (exact v5 name) and request_data \
(its fields). Returns OBS's responseData. Reads (Get*) run immediately; changes ask the user to \
confirm (a "remember" covers that request type for this session); deleting/renaming scenes or \
sources, StartStream and recording start/stop are confirmed every time. Stopping the stream, \
reading the stream key, hotkeys, vendor requests and file-writing screenshots are never possible. \
The privacy scene cannot be changed. Video canvas: read GetVideoSettings (baseWidth/baseHeight, \
usually 1920x1080) before positioning.
Common requests:
- GetSceneList {} -> scenes[{sceneName}], currentProgramSceneName
- CreateScene {sceneName}
- SetCurrentProgramScene {sceneName}
- GetInputList {inputKind?} -> inputs[{inputName,inputKind}]
- GetInputKindList {} (Windows kinds: dshow_input = video capture device/camera, \
browser_source, image_source, text_gdiplus_v3, color_source_v3, monitor_capture, \
window_capture, game_capture, wasapi_input_capture = audio input, wasapi_output_capture = \
desktop audio, ffmpeg_source = media file)
- CreateInput {sceneName, inputName, inputKind, inputSettings?, sceneItemEnabled?} -> \
sceneItemId. browser_source settings: {url, width, height, css}. text_gdiplus_v3: {text, \
font:{face,size}}. color_source_v3: {color (ABGR int), width, height}
- CreateSceneItem {sceneName, sourceName, sceneItemEnabled?} -> sceneItemId (reuse an \
existing source, e.g. the camera, in another scene)
- GetSceneItemList {sceneName} -> sceneItems[{sceneItemId, sourceName, sceneItemIndex}]
- GetSceneItemId {sceneName, sourceName} -> sceneItemId
- SetSceneItemTransform {sceneName, sceneItemId, sceneItemTransform:{positionX, positionY, \
scaleX, scaleY, rotation, alignment (5 = top-left), boundsType ('OBS_BOUNDS_SCALE_INNER' to fit \
a box), boundsWidth, boundsHeight, cropLeft, cropRight, cropTop, cropBottom}}
- GetSceneItemTransform {sceneName, sceneItemId} -> sceneItemTransform (sourceWidth/Height)
- SetSceneItemEnabled {sceneName, sceneItemId, sceneItemEnabled}
- SetSceneItemIndex {sceneName, sceneItemId, sceneItemIndex} (higher = in front)
- SetInputSettings {inputName, inputSettings, overlay?:true}
- SetInputMute {inputName, inputMuted}; SetInputVolume {inputName, inputVolumeDb}
- CreateSourceFilter {sourceName, filterName, filterKind, filterSettings?} (e.g. \
chroma_key_filter_v2, color_filter_v2, noise_suppress_filter_v2, compressor_filter)
- SetSourceFilterEnabled {sourceName, filterName, filterEnabled}
- SetCurrentSceneTransition {transitionName}; SetCurrentSceneTransitionDuration \
{transitionDuration ms}
- GetStreamStatus / GetRecordStatus {} -> outputActive
Example "Just Chatting scene, camera on the left half": GetVideoSettings; CreateScene \
{sceneName:"Just Chatting"}; GetInputList {inputKind:"dshow_input"} to find the camera; \
CreateSceneItem {sceneName:"Just Chatting", sourceName:<camera>}; SetSceneItemTransform with \
positionX 0, positionY 0, boundsType OBS_BOUNDS_SCALE_INNER, boundsWidth baseWidth/2, \
boundsHeight baseHeight; verify with GetSceneItemTransform or obs.screenshot."""

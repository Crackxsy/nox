"""What each obs-websocket request does to the fake OBS, and the events it emits.

A handler is a method named `req_` plus the request in snake case (`GetSceneList` is
`req_get_scene_list`): it takes the request data and a list to append `(eventType, eventData)` to,
and returns the response data or raises `ObsRequestError(code, comment)`. Answers are modelled on
real OBS but are not byte-exact (comments, missing optional fields)."""

from __future__ import annotations

import re
import uuid
from typing import Any

from .protocol import (
    OBS_VERSION,
    OBS_WEBSOCKET_VERSION,
    RPC_VERSION,
    ObsRequestError,
    Status,
    scene_uuid,
)
from .state import FILTER_KINDS, INPUT_KINDS, Input, ObsState, SceneItem, SourceFilter

Events = list[tuple[str, dict[str, Any]]]
_PREFIX = "req_"


def _snake(request_type: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", request_type).lower()


def _camel(handler_name: str) -> str:
    return "".join(part.capitalize() for part in handler_name[len(_PREFIX) :].split("_"))


class ObsRequests:
    """The request handlers over one `ObsState`."""

    def __init__(self, state: ObsState) -> None:
        self.state = state

    def handler(self, request_type: str) -> Any:
        """The method answering `request_type` (`GetSceneList`), or None for one the fake lacks."""
        return getattr(self, _PREFIX + _snake(request_type), None) if request_type else None

    def available(self) -> list[str]:
        return sorted(_camel(n) for n in dir(self) if n.startswith(_PREFIX))

    # -- field helpers ------------------------------------------------------------------------

    @staticmethod
    def _field(data: dict[str, Any], name: str, kind: type = str) -> Any:
        if name not in data:
            raise ObsRequestError(
                Status.MISSING_REQUEST_FIELD, f"Your request is missing the `{name}` field."
            )
        value = data[name]
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise ObsRequestError(
                Status.INVALID_REQUEST_FIELD_TYPE,
                f"Your request has an invalid `{name}` field type.",
            )
        if kind is str and not value:
            raise ObsRequestError(Status.REQUEST_FIELD_EMPTY, f"The `{name}` field is empty.")
        return value

    def _scene(self, name: str) -> list[SceneItem]:
        if name not in self.state.scenes:
            raise ObsRequestError(
                Status.RESOURCE_NOT_FOUND, f"No scene was found by the name of `{name}`."
            )
        return self.state.scenes[name]

    def _input(self, name: str) -> Input:
        if name not in self.state.inputs:
            raise ObsRequestError(
                Status.RESOURCE_NOT_FOUND, f"No input was found by the name of `{name}`."
            )
        return self.state.inputs[name]

    def _source_uuid(self, name: str) -> str:
        found = self.state.inputs.get(name)
        return found.uuid if found is not None else scene_uuid(name)

    def _source_exists(self, name: str) -> bool:
        return name in self.state.inputs or name in self.state.scenes

    # -- requests: general ----------------------------------------------------------------------

    def req_get_version(self, _data: dict[str, Any], _events: Events) -> dict[str, Any]:
        return {
            "obsVersion": OBS_VERSION,
            "obsWebSocketVersion": OBS_WEBSOCKET_VERSION,
            "rpcVersion": RPC_VERSION,
            "availableRequests": self.available(),
            "platform": "windows",
        }

    def req_get_stats(self, _data: dict[str, Any], _events: Events) -> dict[str, Any]:
        return {
            "cpuUsage": 3.5,
            "memoryUsage": 412.0,
            "availableDiskSpace": 250000.0,
            "activeFps": 60.0,
            "averageFrameRenderTime": 1.2,
            "renderSkippedFrames": 0,
            "renderTotalFrames": 18000,
            "outputSkippedFrames": 0,
            "outputTotalFrames": 18000,
        }

    # -- requests: scenes -----------------------------------------------------------------------

    def req_get_scene_list(self, _data: dict[str, Any], _events: Events) -> dict[str, Any]:
        names = self.state.scene_names
        return {
            "currentProgramSceneName": self.state.current_scene,
            "currentPreviewSceneName": None,
            "scenes": [
                {
                    "sceneIndex": len(names) - 1 - i,
                    "sceneName": name,
                    "sceneUuid": scene_uuid(name),
                }
                for i, name in enumerate(names)
            ],
        }

    def req_get_current_program_scene(
        self, _data: dict[str, Any], _events: Events
    ) -> dict[str, Any]:
        return {
            "sceneName": self.state.current_scene,
            "sceneUuid": scene_uuid(self.state.current_scene),
            "currentProgramSceneName": self.state.current_scene,
        }

    def req_set_current_program_scene(
        self, data: dict[str, Any], events: list[tuple[str, dict[str, Any]]]
    ) -> dict[str, Any]:
        name = self._field(data, "sceneName")
        self._scene(name)
        self.state.current_scene = name
        events.append(("CurrentProgramSceneChanged", {"sceneName": name}))
        return {}

    def req_create_scene(
        self, data: dict[str, Any], events: list[tuple[str, dict[str, Any]]]
    ) -> dict[str, Any]:
        name = self._field(data, "sceneName")
        if name in self.state.scenes:
            raise ObsRequestError(
                Status.RESOURCE_ALREADY_EXISTS, f"A scene already exists by the name of `{name}`."
            )
        self.state.scenes[name] = []
        events.append(("SceneCreated", {"sceneName": name, "isGroup": False}))
        events.append(("SceneListChanged", {"scenes": self.req_get_scene_list({}, [])["scenes"]}))
        return {"sceneUuid": str(uuid.uuid4())}

    def req_remove_scene(
        self, data: dict[str, Any], events: list[tuple[str, dict[str, Any]]]
    ) -> dict[str, Any]:
        name = self._field(data, "sceneName")
        self._scene(name)
        if len(self.state.scenes) == 1:
            raise ObsRequestError(Status.NOT_ENOUGH_RESOURCES, "OBS needs at least one scene.")
        del self.state.scenes[name]
        if self.state.current_scene == name:
            self.state.current_scene = next(iter(self.state.scenes))
            events.append(("CurrentProgramSceneChanged", {"sceneName": self.state.current_scene}))
        events.append(("SceneRemoved", {"sceneName": name, "isGroup": False}))
        return {}

    def req_get_scene_item_list(self, data: dict[str, Any], _events: Events) -> dict[str, Any]:
        items = self._scene(self._field(data, "sceneName"))
        return {
            "sceneItems": [
                {
                    "sceneItemId": item.item_id,
                    "sceneItemIndex": index,
                    "sourceName": item.source_name,
                    "sourceUuid": self._source_uuid(item.source_name),
                    "sourceType": (
                        "OBS_SOURCE_TYPE_SCENE"
                        if item.source_name in self.state.scenes
                        else "OBS_SOURCE_TYPE_INPUT"
                    ),
                    "sceneItemEnabled": item.enabled,
                    "isGroup": None,
                }
                for index, item in enumerate(items)
            ]
        }

    def req_get_scene_item_id(self, data: dict[str, Any], _events: Events) -> dict[str, Any]:
        items = self._scene(self._field(data, "sceneName"))
        source = self._field(data, "sourceName")
        for item in items:
            if item.source_name == source:
                return {"sceneItemId": item.item_id}
        raise ObsRequestError(
            Status.RESOURCE_NOT_FOUND,
            f"No scene items were found in the scene by the name `{source}`.",
        )

    def req_create_scene_item(
        self, data: dict[str, Any], events: list[tuple[str, dict[str, Any]]]
    ) -> dict[str, Any]:
        items = self._scene(self._field(data, "sceneName"))
        source = self._field(data, "sourceName")
        if not self._source_exists(source):
            raise ObsRequestError(
                Status.RESOURCE_NOT_FOUND, f"No source was found by the name of `{source}`."
            )
        item = SceneItem(
            self.state.next_item_id(), source, bool(data.get("sceneItemEnabled", True))
        )
        items.append(item)
        events.append(
            (
                "SceneItemCreated",
                {"sceneName": data["sceneName"], "sourceName": source, "sceneItemId": item.item_id},
            )
        )
        return {"sceneItemId": item.item_id}

    def req_set_scene_item_enabled(
        self, data: dict[str, Any], events: list[tuple[str, dict[str, Any]]]
    ) -> dict[str, Any]:
        scene = self._field(data, "sceneName")
        item_id = self._field(data, "sceneItemId", int)
        enabled = self._field(data, "sceneItemEnabled", bool)
        for item in self._scene(scene):
            if item.item_id == item_id:
                item.enabled = enabled
                events.append(
                    (
                        "SceneItemEnableStateChanged",
                        {"sceneName": scene, "sceneItemId": item_id, "sceneItemEnabled": enabled},
                    )
                )
                return {}
        raise ObsRequestError(
            Status.RESOURCE_NOT_FOUND,
            f"No scene items were found in scene `{scene}` with id {item_id}.",
        )

    # -- requests: inputs -----------------------------------------------------------------------

    def req_get_input_list(self, data: dict[str, Any], _events: Events) -> dict[str, Any]:
        kind = data.get("inputKind")
        return {
            "inputs": [
                {
                    "inputName": i.name,
                    "inputKind": i.kind,
                    "inputUuid": i.uuid,
                    "unversionedInputKind": i.kind,
                }
                for i in self.state.inputs.values()
                if kind is None or i.kind == kind
            ]
        }

    def req_create_input(
        self, data: dict[str, Any], events: list[tuple[str, dict[str, Any]]]
    ) -> dict[str, Any]:
        scene = self._field(data, "sceneName")
        items = self._scene(scene)
        name = self._field(data, "inputName")
        kind = self._field(data, "inputKind")
        if kind not in INPUT_KINDS:
            raise ObsRequestError(Status.INVALID_INPUT_KIND, f"Unknown input kind `{kind}`.")
        if self._source_exists(name):
            raise ObsRequestError(
                Status.RESOURCE_ALREADY_EXISTS, f"A source already exists by the name of `{name}`."
            )
        created = Input(name, kind, dict(data.get("inputSettings", {}) or {}))
        self.state.inputs[name] = created
        item = SceneItem(self.state.next_item_id(), name, bool(data.get("sceneItemEnabled", True)))
        items.append(item)
        events.append(
            ("InputCreated", {"inputName": name, "inputKind": kind, "inputUuid": created.uuid})
        )
        events.append(
            (
                "SceneItemCreated",
                {"sceneName": scene, "sourceName": name, "sceneItemId": item.item_id},
            )
        )
        return {"inputUuid": created.uuid, "sceneItemId": item.item_id}

    def req_remove_input(
        self, data: dict[str, Any], events: list[tuple[str, dict[str, Any]]]
    ) -> dict[str, Any]:
        name = self._field(data, "inputName")
        removed = self._input(name)
        del self.state.inputs[name]
        self.state.filters.pop(name, None)
        for items in self.state.scenes.values():
            items[:] = [item for item in items if item.source_name != name]
        events.append(("InputRemoved", {"inputName": name, "inputUuid": removed.uuid}))
        return {}

    def req_get_input_settings(self, data: dict[str, Any], _events: Events) -> dict[str, Any]:
        found = self._input(self._field(data, "inputName"))
        return {"inputSettings": dict(found.settings), "inputKind": found.kind}

    def req_set_input_settings(
        self, data: dict[str, Any], events: list[tuple[str, dict[str, Any]]]
    ) -> dict[str, Any]:
        found = self._input(self._field(data, "inputName"))
        settings = self._field(data, "inputSettings", dict)
        if data.get("overlay", True):
            found.settings.update(settings)
        else:
            found.settings = dict(settings)
        events.append(
            (
                "InputSettingsChanged",
                {"inputName": found.name, "inputSettings": dict(found.settings)},
            )
        )
        return {}

    def req_get_input_mute(self, data: dict[str, Any], _events: Events) -> dict[str, Any]:
        return {"inputMuted": self._input(self._field(data, "inputName")).muted}

    def req_set_input_mute(
        self, data: dict[str, Any], events: list[tuple[str, dict[str, Any]]]
    ) -> dict[str, Any]:
        found = self._input(self._field(data, "inputName"))
        found.muted = self._field(data, "inputMuted", bool)
        events.append(
            ("InputMuteStateChanged", {"inputName": found.name, "inputMuted": found.muted})
        )
        return {}

    def req_get_input_properties_list_property_items(
        self, data: dict[str, Any], _events: Events
    ) -> dict[str, Any]:
        self._input(self._field(data, "inputName"))
        self._field(data, "propertyName")
        return {
            "propertyItems": [
                {"itemName": name, "itemValue": value, "itemEnabled": True}
                for name, value in self.state.audio_devices
            ]
        }

    def req_set_input_audio_tracks(self, data: dict[str, Any], _events: Events) -> dict[str, Any]:
        found = self._input(self._field(data, "inputName"))
        tracks = self._field(data, "inputAudioTracks", dict)
        self.state.audio_tracks[found.name] = {str(k): bool(v) for k, v in tracks.items()}
        return {}

    # -- requests: filters ----------------------------------------------------------------------

    def req_get_source_filter_list(self, data: dict[str, Any], _events: Events) -> dict[str, Any]:
        source = self._field(data, "sourceName")
        if not self._source_exists(source):
            raise ObsRequestError(
                Status.RESOURCE_NOT_FOUND, f"No source was found by the name of `{source}`."
            )
        return {
            "filters": [
                {
                    "filterIndex": index,
                    "filterName": f.name,
                    "filterKind": f.kind,
                    "filterEnabled": f.enabled,
                    "filterSettings": dict(f.settings),
                }
                for index, f in enumerate(self.state.filters.get(source, []))
            ]
        }

    def req_create_source_filter(
        self, data: dict[str, Any], events: list[tuple[str, dict[str, Any]]]
    ) -> dict[str, Any]:
        source = self._field(data, "sourceName")
        if not self._source_exists(source):
            raise ObsRequestError(
                Status.RESOURCE_NOT_FOUND, f"No source was found by the name of `{source}`."
            )
        name = self._field(data, "filterName")
        kind = self._field(data, "filterKind")
        if kind not in FILTER_KINDS:
            raise ObsRequestError(Status.INVALID_FILTER_KIND, f"Unknown filter kind `{kind}`.")
        existing = self.state.filters.setdefault(source, [])
        if any(f.name == name for f in existing):
            raise ObsRequestError(
                Status.RESOURCE_ALREADY_EXISTS, f"A filter already exists by the name of `{name}`."
            )
        existing.append(SourceFilter(name, kind, True, dict(data.get("filterSettings", {}) or {})))
        events.append(
            ("SourceFilterCreated", {"sourceName": source, "filterName": name, "filterKind": kind})
        )
        return {}

    def _filter(self, data: dict[str, Any]) -> SourceFilter:
        source = self._field(data, "sourceName")
        name = self._field(data, "filterName")
        for candidate in self.state.filters.get(source, []):
            if candidate.name == name:
                return candidate
        raise ObsRequestError(
            Status.RESOURCE_NOT_FOUND, f"No filter was found by the name of `{name}`."
        )

    def req_set_source_filter_enabled(
        self, data: dict[str, Any], events: list[tuple[str, dict[str, Any]]]
    ) -> dict[str, Any]:
        found = self._filter(data)
        found.enabled = self._field(data, "filterEnabled", bool)
        events.append(
            (
                "SourceFilterEnableStateChanged",
                {
                    "sourceName": data["sourceName"],
                    "filterName": found.name,
                    "filterEnabled": found.enabled,
                },
            )
        )
        return {}

    def req_remove_source_filter(
        self, data: dict[str, Any], events: list[tuple[str, dict[str, Any]]]
    ) -> dict[str, Any]:
        found = self._filter(data)
        self.state.filters[data["sourceName"]].remove(found)
        events.append(
            ("SourceFilterRemoved", {"sourceName": data["sourceName"], "filterName": found.name})
        )
        return {}

    # -- requests: outputs ----------------------------------------------------------------------

    def req_get_stream_status(self, _data: dict[str, Any], _events: Events) -> dict[str, Any]:
        return {
            "outputActive": self.state.streaming,
            "outputReconnecting": False,
            "outputTimecode": "00:00:00.000" if not self.state.streaming else "00:12:31.250",
            "outputDuration": 0 if not self.state.streaming else 751250,
            "outputCongestion": 0.0,
            "outputBytes": 0,
            "outputSkippedFrames": 0,
            "outputTotalFrames": 0,
        }

    def req_start_stream(
        self, _data: dict[str, Any], events: list[tuple[str, dict[str, Any]]]
    ) -> dict[str, Any]:
        if self.state.streaming:
            raise ObsRequestError(Status.OUTPUT_RUNNING, "The stream is already running.")
        self.state.streaming = True
        events.append(
            (
                "StreamStateChanged",
                {"outputActive": True, "outputState": "OBS_WEBSOCKET_OUTPUT_STARTING"},
            )
        )
        events.append(
            (
                "StreamStateChanged",
                {"outputActive": True, "outputState": "OBS_WEBSOCKET_OUTPUT_STARTED"},
            )
        )
        return {}

    def req_stop_stream(
        self, _data: dict[str, Any], events: list[tuple[str, dict[str, Any]]]
    ) -> dict[str, Any]:
        if not self.state.streaming:
            raise ObsRequestError(Status.OUTPUT_NOT_RUNNING, "The stream is not running.")
        self.state.streaming = False
        events.append(
            (
                "StreamStateChanged",
                {"outputActive": False, "outputState": "OBS_WEBSOCKET_OUTPUT_STOPPING"},
            )
        )
        events.append(
            (
                "StreamStateChanged",
                {"outputActive": False, "outputState": "OBS_WEBSOCKET_OUTPUT_STOPPED"},
            )
        )
        return {}

    def req_get_record_status(self, _data: dict[str, Any], _events: Events) -> dict[str, Any]:
        return {
            "outputActive": self.state.recording,
            "outputPaused": self.state.recording_paused,
            "outputTimecode": "00:00:00.000",
            "outputDuration": 0,
            "outputBytes": 0,
        }

    def req_start_record(
        self, _data: dict[str, Any], events: list[tuple[str, dict[str, Any]]]
    ) -> dict[str, Any]:
        if self.state.recording:
            raise ObsRequestError(Status.OUTPUT_RUNNING, "The recording is already running.")
        self.state.recording = True
        events.append(
            (
                "RecordStateChanged",
                {
                    "outputActive": True,
                    "outputState": "OBS_WEBSOCKET_OUTPUT_STARTED",
                    "outputPath": None,
                },
            )
        )
        return {}

    def req_stop_record(
        self, _data: dict[str, Any], events: list[tuple[str, dict[str, Any]]]
    ) -> dict[str, Any]:
        if not self.state.recording:
            raise ObsRequestError(Status.OUTPUT_NOT_RUNNING, "The recording is not running.")
        self.state.recording = False
        path = "recordings/example-2026-01-01.mkv"
        events.append(
            (
                "RecordStateChanged",
                {
                    "outputActive": False,
                    "outputState": "OBS_WEBSOCKET_OUTPUT_STOPPED",
                    "outputPath": path,
                },
            )
        )
        return {"outputPath": path}

    def req_get_replay_buffer_status(
        self, _data: dict[str, Any], _events: Events
    ) -> dict[str, Any]:
        return {"outputActive": self.state.replay_buffer_active}

    def req_save_replay_buffer(
        self, _data: dict[str, Any], events: list[tuple[str, dict[str, Any]]]
    ) -> dict[str, Any]:
        if not self.state.replay_buffer_active:
            raise ObsRequestError(Status.OUTPUT_NOT_RUNNING, "The replay buffer is not running.")
        events.append(("ReplayBufferSaved", {"savedReplayPath": "replays/example-replay-0001.mkv"}))
        return {}

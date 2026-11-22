"""The OBS behind the fake server: scenes with their items, inputs, filters and the outputs.

Tests read it to see what a client did; requests change it (`requests.ObsRequests`)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

#: Input kinds the fake accepts in `CreateInput`; anything else is `InvalidInputKind` (605).
INPUT_KINDS = frozenset(
    {
        "browser_source",
        "text_gdiplus_v3",
        "image_source",
        "color_source_v3",
        "wasapi_input_capture",
        "wasapi_output_capture",
        "wasapi_process_output_capture",
        "game_capture",
        "window_capture",
        "monitor_capture",
        "dshow_input",
        "ffmpeg_source",
    }
)
#: Filter kinds the fake accepts in `CreateSourceFilter`; anything else is 607.
FILTER_KINDS = frozenset(
    {
        "color_filter_v2",
        "gain_filter",
        "noise_suppress_filter_v2",
        "compressor_filter",
        "limiter_filter",
        "render_delay",
        "gpu_delay",
        "chroma_key_filter_v2",
        "mask_filter_v2",
        "scroll_filter",
        "sharpness_filter_v2",
    }
)


@dataclass
class SceneItem:
    item_id: int
    source_name: str
    enabled: bool = True


@dataclass
class Input:
    name: str
    kind: str
    settings: dict[str, Any] = field(default_factory=dict)
    muted: bool = False
    uuid: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class SourceFilter:
    name: str
    kind: str
    enabled: bool = True
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass
class ObsState:
    """The OBS behind the server. Tests read it; requests change it."""

    scenes: dict[str, list[SceneItem]] = field(default_factory=dict)
    current_scene: str = ""
    inputs: dict[str, Input] = field(default_factory=dict)
    filters: dict[str, list[SourceFilter]] = field(default_factory=dict)
    streaming: bool = False
    recording: bool = False
    recording_paused: bool = False
    replay_buffer_active: bool = True
    #: What `GetInputPropertiesListPropertyItems` lists for an audio capture input.
    audio_devices: list[tuple[str, str]] = field(
        default_factory=lambda: [("Studio Microphone", "mic-0001"), ("Virtual Cable", "cable-0002")]
    )
    #: `inputName -> {track number: enabled}`, as set by `SetInputAudioTracks`.
    audio_tracks: dict[str, dict[str, bool]] = field(default_factory=dict)
    _next_item_id: int = 1

    def next_item_id(self) -> int:
        item_id = self._next_item_id
        self._next_item_id += 1
        return item_id

    @property
    def scene_names(self) -> list[str]:
        return list(self.scenes)

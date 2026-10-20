"""Nox's own audio track in OBS, in one step.

With VB-Audio Virtual Cable installed, Windows has a playback device "CABLE Input" and a recording
device "CABLE Output" that hears whatever is played into it. `AudioTrackService.setup`:

1. finds the cable among the output devices the voice worker sees,
2. asks the `obs` plugin - permission-checked, one confirmation - to create the "Nox Voice" audio
   input on "CABLE Output" (optionally routed to chosen OBS tracks),
3. only then points Nox's stream device at "CABLE Input" and sets the routing to `both`, so Nox
   is heard on that OBS source and, as before, on the headphones.

Declining the confirmation, or OBS not being there, leaves the voice settings as they were.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from pydantic import BaseModel, Field, field_validator

from nox.core.logging import get_logger
from nox.tools.executor import ToolExecutor

log = get_logger(__name__)

#: How the playback side of a virtual cable is named (VB-Audio: "CABLE Input (...)").
CABLE_PLAYBACK_HINTS = ("cable input", "voicemeeter input", "voicemeeter aux input")
#: And the recording side OBS captures.
CABLE_RECORDING_HINT = "CABLE Output"


class AudioTrackRequest(BaseModel):
    """`stream.audio_track.setup` (IPC and tool)."""

    #: OBS audio tracks (1-6) the Nox source goes to; empty keeps OBS's default (all).
    tracks: list[int] = Field(default_factory=list, max_length=6)
    scene_name: str = Field(default="", max_length=200)

    @field_validator("tracks")
    @classmethod
    def _track_numbers(cls, value: list[int]) -> list[int]:
        if any(t < 1 or t > 6 for t in value):
            raise ValueError("OBS audio tracks are numbered 1 to 6")
        return sorted(set(value))


class _Editor(Protocol):
    async def apply(
        self, values: Mapping[str, Any], *, by: str = ..., pin: str | None = ...
    ) -> dict[str, Any]: ...


DeviceLister = Callable[[], Awaitable[list[str] | None]]


def find_cable(devices: list[str]) -> str | None:
    """The playback side of a virtual cable among `devices`, or None."""
    for hint in CABLE_PLAYBACK_HINTS:
        for name in devices:
            if hint in name.casefold():
                return name
    return None


class AudioTrackService:
    def __init__(
        self,
        executor: ToolExecutor,
        *,
        devices: DeviceLister,
        editor: Callable[[], _Editor | None],
        mode: Callable[[], str],
    ) -> None:
        self._executor = executor
        self._devices = devices
        self._editor = editor
        self._mode = mode

    async def setup(self, request: AudioTrackRequest, *, agent: str, origin: str) -> dict[str, Any]:
        devices = await self._devices()
        if devices is None:
            return {"ok": False, "reason": "the audio devices could not be listed"}
        cable = find_cable(devices)
        if cable is None:
            return {
                "ok": False,
                "reason": (
                    "no virtual audio cable found - install VB-Audio Virtual Cable "
                    "(vb-audio.com/Cable), then try again"
                ),
            }
        result = await self._executor.call(
            agent,
            "obs.audio_track.setup",
            {
                "device_hint": CABLE_RECORDING_HINT,
                "scene_name": request.scene_name,
                "tracks": request.tracks,
            },
            mode=self._mode(),
            origin=origin,
        )
        data = dict(result.data or {})
        if not result.ok or not data.get("ok", False):
            return {
                "ok": False,
                "reason": str(data.get("reason") or result.error or "OBS was not set up"),
            }
        editor = self._editor()
        if editor is None:
            return {"ok": False, "reason": "settings are not available in this core"}
        applied = await editor.apply(
            {"voice.channels.stream_device": cable, "voice.channels.routing": "both"},
            by=agent,
        )
        log.info("stream.audio_track_set_up", device=cable, scene=data.get("scene", ""))
        return {
            "ok": bool(applied.get("ok", True)),
            "obs_source": data.get("input_name", ""),
            "scene": data.get("scene", ""),
            "nox_plays_to": cable,
            "routing": "both",
            "tracks": request.tracks,
        }

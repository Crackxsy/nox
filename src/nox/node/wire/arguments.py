"""Strict argument models of the tools the core and a node both name.

The core validates a tool call against these before it signs a receipt and the node validates it
again before a handler runs (`extra="forbid"`: nothing the other side sends beyond the fields
is accepted). One definition on both sides means the two can never disagree about a field.
The file tools' arguments stay with their refusal rules in `nox.node.device.hands.args`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "NO_SCENE_SET",
    "MediaAction",
    "MediaArgs",
    "NoArgs",
    "NotifyArgs",
    "ObsArgs",
    "SayArgs",
]


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NotifyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="Nox", min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=400)


class SayArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(
        min_length=1, max_length=300, description="What to say, one or two sentences."
    )
    language: Literal["de", "en"] = "de"


MediaAction = Literal[
    "play", "pause", "toggle", "next", "previous", "volume", "louder", "quieter", "mute", "unmute"
]


class MediaArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: MediaAction = Field(
        description="play, pause, toggle, next, previous, volume (with level), louder, quieter, "
        "mute or unmute."
    )
    level: int | None = Field(default=None, ge=0, le=100, description="Volume in percent.")

    @model_validator(mode="after")
    def _level_for_volume(self) -> MediaArgs:
        if (self.action == "volume") != (self.level is not None):
            raise ValueError("'level' belongs to the action 'volume', and only to it")
        return self


#: A scene set no real scene is in: without a scene set here, every scene switch is high.
NO_SCENE_SET = "\x00"


class ObsArgs(BaseModel):
    """`obs.request` on a node: the core's input (one obs-websocket v5 request), strict."""

    model_config = ConfigDict(extra="forbid")

    request_type: str = Field(min_length=3, max_length=64, pattern=r"^[A-Z][A-Za-z0-9]+$")
    request_data: dict[str, Any] = Field(default_factory=dict)

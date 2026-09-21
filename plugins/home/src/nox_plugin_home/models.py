"""Input models for the `home.*` tools, and the Home Assistant service each one maps onto.

Every model is validated by the core before a handler runs, so the bounds here are real
protection and not documentation: a brightness of 400 % or a target temperature of 90 °C is
rejected before anything reaches the house.

The service mapping lives next to the models on purpose. It is the complete list of what this
plugin can ask Home Assistant to do - eleven services across six domains - and keeping it in one
readable table is what makes "Nox cannot unlock your door" checkable rather than claimed.
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, Field

#: Upper bound on one call's target list. A command is a command, not a house-wide broadcast; a
#: bigger set is a sign the caller resolved the wrong thing.
MAX_TARGETS: Final[int] = 50

EntityId = Annotated[str, Field(min_length=3, max_length=255, pattern=r"^[a-z_]+\.[a-z0-9_]+$")]
EntityIds = Annotated[list[EntityId], Field(min_length=1, max_length=MAX_TARGETS)]


class EmptyInput(BaseModel):
    """Tools that take no arguments."""


class ListInput(BaseModel):
    """`home.list`: optionally narrowed to one area and/or one domain."""

    area: str = Field(default="", max_length=120)
    domain: str = Field(default="", max_length=40)


class StateInput(BaseModel):
    """`home.state`: one entity."""

    entity_id: EntityId


class LightInput(BaseModel):
    """`home.light`. `on=False` switches off; everything else implies switching on.

    `brightness_pct` is absolute, `brightness_step_pct` relative - Home Assistant has both, and a
    spoken "etwas heller" is the relative one.
    """

    entity_ids: EntityIds
    on: bool | None = None
    brightness_pct: int | None = Field(default=None, ge=0, le=100)
    brightness_step_pct: int | None = Field(default=None, ge=-100, le=100)
    color_temp_kelvin: int | None = Field(default=None, ge=1500, le=6600)
    transition_s: float | None = Field(default=None, ge=0.0, le=30.0)


class SwitchInput(BaseModel):
    """`home.switch`."""

    entity_ids: EntityIds
    on: bool


class SceneInput(BaseModel):
    """`home.scene`: activate one scene the user defined in Home Assistant."""

    entity_id: EntityId


MediaAction = Literal[
    "play", "pause", "next", "previous", "volume_set", "volume_up", "volume_down", "mute", "unmute"
]


class MediaInput(BaseModel):
    """`home.media`. `volume_pct` is required for `volume_set` and ignored otherwise."""

    entity_ids: EntityIds
    action: MediaAction
    volume_pct: int | None = Field(default=None, ge=0, le=100)


class ClimateInput(BaseModel):
    """`home.climate`: target temperature only.

    The bounds are the range a home thermostat plausibly accepts. Mode switching (heat/cool/off)
    is not exposed: it is the setting most likely to be wrong in a way nobody notices until the
    pipes freeze.
    """

    entity_ids: EntityIds
    temperature_c: float = Field(ge=4.0, le=35.0)


CoverAction = Literal["open", "close", "stop", "set_position"]


class CoverInput(BaseModel):
    """`home.cover`: blinds and curtains. Garage doors, gates and entrance doors are refused by
    `nox.home.boundary` before this model is ever consulted."""

    entity_ids: EntityIds
    action: CoverAction
    position_pct: int | None = Field(default=None, ge=0, le=100)


class ScriptInput(BaseModel):
    """`home.script` / `home.automation.trigger`: one script or automation entity."""

    entity_id: EntityId


#: Media action -> (Home Assistant service, extra service data builder).
MEDIA_SERVICES: Final[dict[str, str]] = {
    "play": "media_play",
    "pause": "media_pause",
    "next": "media_next_track",
    "previous": "media_previous_track",
    "volume_set": "volume_set",
    "volume_up": "volume_up",
    "volume_down": "volume_down",
    "mute": "volume_mute",
    "unmute": "volume_mute",
}

#: Cover action -> Home Assistant service.
COVER_SERVICES: Final[dict[str, str]] = {
    "open": "open_cover",
    "close": "close_cover",
    "stop": "stop_cover",
    "set_position": "set_cover_position",
}


def light_service(data: LightInput) -> tuple[str, dict[str, Any]]:
    """The `light.*` service and service data for one `home.light` call."""
    if data.on is False:
        payload: dict[str, Any] = {}
        if data.transition_s is not None:
            payload["transition"] = data.transition_s
        return "turn_off", payload
    payload = {}
    if data.brightness_pct is not None:
        payload["brightness_pct"] = data.brightness_pct
    if data.brightness_step_pct is not None:
        payload["brightness_step_pct"] = data.brightness_step_pct
    if data.color_temp_kelvin is not None:
        payload["color_temp_kelvin"] = data.color_temp_kelvin
    if data.transition_s is not None:
        payload["transition"] = data.transition_s
    return "turn_on", payload


def media_service(data: MediaInput) -> tuple[str, dict[str, Any]]:
    """The `media_player.*` service and service data for one `home.media` call."""
    service = MEDIA_SERVICES[data.action]
    if data.action == "volume_set":
        volume = 0 if data.volume_pct is None else data.volume_pct
        return service, {"volume_level": round(volume / 100, 3)}
    if data.action in ("mute", "unmute"):
        return service, {"is_volume_muted": data.action == "mute"}
    return service, {}


def cover_service(data: CoverInput) -> tuple[str, dict[str, Any]]:
    """The `cover.*` service and service data for one `home.cover` call."""
    service = COVER_SERVICES[data.action]
    if data.action == "set_position":
        position = 0 if data.position_pct is None else data.position_pct
        return service, {"position": position}
    return service, {}

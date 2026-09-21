"""Turning Home Assistant's four lists into the one list Nox shows.

Home Assistant keeps states, an area registry, a device registry and an entity registry apart. The
dashboard and the intent layer want a single flat row per entity: id, the name the user gave it,
the room it is in and its current state. This module joins them, and does three other things that
matter more than the join:

* it drops everything on the wrong side of `nox.home.boundary` - locks, alarm panels, valves,
  garage-door covers and the person/device-tracker domains never appear in the result at all, so
  no later layer has to remember to filter them;
* it keeps only an **allow-list** of attributes (`ATTRIBUTE_ALLOWLIST`). A media player's
  `media_title` says what someone is watching, and that is not something Nox needs in order to
  press pause;
* it degrades honestly. The three registries need an admin token; without one the join falls back
  to friendly names with no area, and the caller reports `areas_available: false` rather than
  inventing rooms.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from nox.home.boundary import forbidden_reason, is_exposed

#: Entity attributes that may leave Home Assistant. Everything else stays there - an allow-list,
#: because the interesting failure is the attribute nobody thought about.
ATTRIBUTE_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "device_class",
        "unit_of_measurement",
        "supported_features",
        "supported_color_modes",
        "brightness",
        "color_temp_kelvin",
        "min_color_temp_kelvin",
        "max_color_temp_kelvin",
        "current_temperature",
        "temperature",
        "min_temp",
        "max_temp",
        "hvac_action",
        "hvac_modes",
        "volume_level",
        "is_volume_muted",
        "current_position",
    }
)


@dataclass(frozen=True, slots=True)
class EntityRow:
    """One entity as every layer above this one sees it."""

    entity_id: str
    name: str
    area: str
    state: str
    attributes: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "name": self.name,
            "area": self.area,
            "state": self.state,
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True, slots=True)
class Inventory:
    """The joined, filtered view of one Home Assistant instance."""

    entities: tuple[EntityRow, ...] = ()
    areas: tuple[str, ...] = ()
    #: False when the registries could not be read (a non-admin token); areas are then unknown,
    #: not empty-because-there-are-none.
    areas_available: bool = True
    #: `entity_id -> why it is off limits`, for the entities the boundary dropped. Nothing about
    #: them is ever reported upwards; this exists so a caller that names one explicitly gets the
    #: real reason ("that is a garage door") instead of "unknown entity".
    hidden: Mapping[str, str] = field(default_factory=dict)

    def by_id(self, entity_id: str) -> EntityRow | None:
        for row in self.entities:
            if row.entity_id == entity_id:
                return row
        return None

    def hidden_reason(self, entity_id: str) -> str | None:
        return self.hidden.get(entity_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "entities": [row.as_dict() for row in self.entities],
            "areas": list(self.areas),
            "areas_available": self.areas_available,
        }


def filter_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    """The allow-listed subset of one entity's attributes."""
    if not attributes:
        return {}
    return {key: value for key, value in attributes.items() if key in ATTRIBUTE_ALLOWLIST}


def _friendly_name(state: Mapping[str, Any], registry_name: str, entity_id: str) -> str:
    attributes = state.get("attributes") or {}
    name = str(attributes.get("friendly_name") or "").strip()
    if name:
        return name
    if registry_name:
        return registry_name
    return entity_id.partition(".")[2].replace("_", " ").strip() or entity_id


def _area_index(areas: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    return {
        str(row.get("area_id", "")): str(row.get("name", ""))
        for row in areas
        if row.get("area_id") and row.get("name")
    }


def _device_areas(devices: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    return {
        str(row.get("id", "")): str(row.get("area_id") or "") for row in devices if row.get("id")
    }


@dataclass(frozen=True, slots=True)
class _RegistryEntry:
    area_id: str
    device_id: str
    name: str


def _entity_index(entities: Iterable[Mapping[str, Any]]) -> dict[str, _RegistryEntry]:
    index: dict[str, _RegistryEntry] = {}
    for row in entities:
        entity_id = str(row.get("entity_id", ""))
        if not entity_id:
            continue
        index[entity_id] = _RegistryEntry(
            area_id=str(row.get("area_id") or ""),
            device_id=str(row.get("device_id") or ""),
            name=str(row.get("name") or row.get("original_name") or ""),
        )
    return index


def build_inventory(
    states: Sequence[Mapping[str, Any]],
    *,
    areas: Sequence[Mapping[str, Any]] | None = None,
    devices: Sequence[Mapping[str, Any]] | None = None,
    entities: Sequence[Mapping[str, Any]] | None = None,
) -> Inventory:
    """Join `get_states` with the three registries; `None` registries mean "could not read them"."""
    available = areas is not None and devices is not None and entities is not None
    area_names = _area_index(areas or ())
    device_area = _device_areas(devices or ())
    registry = _entity_index(entities or ())

    rows: list[EntityRow] = []
    used_areas: dict[str, None] = {}
    hidden: dict[str, str] = {}
    for state in states:
        entity_id = str(state.get("entity_id", ""))
        attributes = state.get("attributes") or {}
        if not entity_id:
            continue
        reason = forbidden_reason(entity_id, attributes)
        if reason is not None:
            hidden[entity_id] = reason
            continue
        if not is_exposed(entity_id, attributes):
            continue
        entry = registry.get(entity_id)
        area_id = entry.area_id if entry is not None else ""
        if not area_id and entry is not None and entry.device_id:
            area_id = device_area.get(entry.device_id, "")
        area = area_names.get(area_id, "")
        if area:
            used_areas.setdefault(area, None)
        rows.append(
            EntityRow(
                entity_id=entity_id,
                name=_friendly_name(state, entry.name if entry else "", entity_id),
                area=area,
                state=str(state.get("state", "")),
                attributes=filter_attributes(attributes),
            )
        )
    rows.sort(key=lambda row: (row.area, row.name, row.entity_id))
    return Inventory(
        entities=tuple(rows),
        areas=tuple(sorted(used_areas)),
        areas_available=available,
        hidden=hidden,
    )

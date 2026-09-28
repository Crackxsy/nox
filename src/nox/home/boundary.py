"""The hard boundary of what Nox may see and do in a home, in one place.

A language model must not be able to unlock a door. That is not a prompt instruction and not a
permission rule that a profile could relax: the entity domains that open a house are refused in
code, before any service call is built, and the refusal is logged with the entity that caused it.

Three lists make up the boundary:

* :data:`FORBIDDEN_DOMAINS` - Home Assistant domains Nox never reads and never calls a service on.
  Locks, alarm panels and valves. There is no tool for them, they are filtered out of every
  listing, and naming one explicitly is an error, not a denied permission.
* :data:`FORBIDDEN_COVER_DEVICE_CLASSES` - `cover` is exposed for blinds, and a garage door is a
  `cover` too. A cover that reports itself as a garage, a gate or a door is therefore treated like
  a lock.
* :data:`CONTROLLABLE_DOMAINS` / :data:`READABLE_DOMAINS` - the allow-list. Anything that is on
  neither list is invisible to Nox, so a domain Home Assistant adds in a future release is not
  silently reachable.

The lists above are about the entity a tool names. A scene, a script or an automation names one
entity and changes others, and a relay that opens a garage is often a plain `switch`. So there is
a second question, :func:`entity_effect` / :func:`config_effect` / :func:`members_effect`: what
does this call *actually* touch? The answer is one of three :class:`Effect` values - `safe`,
`confirm` (the user has to confirm, and the confirmation names the entities) or `forbidden`
(refused like a lock). The `home` plugin answers it for every scene, script, automation, switch
and cover call through the core's tool preflight, so the decision is made before the permission
engine runs, not after.

Both halves of the feature import this module - the `home` plugin worker that talks to Home
Assistant and the core-side intent layer - so the two can never drift apart.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

__all__ = [
    "CONTROLLABLE_DOMAINS",
    "DOMAIN_TOOLS",
    "ENTRANCE_DEVICE_CLASSES",
    "SAFE_COVER_DEVICE_CLASSES",
    "ConfigReferences",
    "Effect",
    "EffectFinding",
    "config_effect",
    "config_references",
    "entity_effect",
    "looks_like_entrance",
    "members_effect",
    "FORBIDDEN_COVER_DEVICE_CLASSES",
    "FORBIDDEN_DOMAINS",
    "PRIVATE_DOMAINS",
    "READABLE_DOMAINS",
    "ForbiddenEntityError",
    "domain_of",
    "forbidden_reason",
    "is_exposed",
    "require_allowed",
]

#: Domains that secure a building. Never listed, never read, never actuated - a hard boundary, not
#: a risk level, because "confirm before unlocking" is still a door an AI layer can ask to open.
FORBIDDEN_DOMAINS: Final[frozenset[str]] = frozenset(
    {
        "lock",  # door locks
        "alarm_control_panel",  # intruder alarms
        "valve",  # water/gas valves
    }
)

#: `cover` carries blinds *and* garage doors. These device classes are the second kind.
FORBIDDEN_COVER_DEVICE_CLASSES: Final[frozenset[str]] = frozenset({"garage", "gate", "door"})

#: `cover` device classes that are known to be window coverings. A cover without one of these - no
#: device class at all, or a `window` or `damper` - is not refused, but every call on it needs a
#: confirmation: an unclassified cover is how a garage door often shows up.
SAFE_COVER_DEVICE_CLASSES: Final[frozenset[str]] = frozenset(
    {"awning", "blind", "curtain", "shade", "shutter"}
)

#: `switch` device classes some integrations report for an entrance relay.
ENTRANCE_DEVICE_CLASSES: Final[frozenset[str]] = frozenset({"garage", "gate", "door"})

#: Name fragments (German and English) of a relay that opens something. A heuristic, stated as one:
#: a garage relay called `relay_3` is not caught, and a confirmation for a false positive costs one
#: click. Matched against the lower-case entity id and friendly name.
ENTRANCE_NAME_FRAGMENTS: Final[tuple[str, ...]] = (
    "garage",
    "gate",
    "door",
    "einfahrt",
    "schranke",
    "pforte",
    "opener",
    "oeffner",
    "öffner",
    "tuer",
    "tür",
)

#: Words that contain a fragment above without meaning an entrance ("indoor lights").
_NOT_AN_ENTRANCE: Final[tuple[str, ...]] = ("indoor", "outdoor")

#: "Tor" (gate) as a word of its own or as the end of a German compound (`Hoftor`, `Gartentor`),
#: without matching `motor` or `monitor`.
_TOR_RE: Final[re.Pattern[str]] = re.compile(
    r"(^|[^a-zäöü])([a-zäöü]*(hof|garten|ein))?tor($|[^a-zäöü])"
)

#: Domains Nox excludes because they describe people rather than devices: who is home and where a
#: phone is. Not a safety boundary - a privacy one (see docs/PRIVACY.md).
PRIVATE_DOMAINS: Final[frozenset[str]] = frozenset({"person", "device_tracker"})

#: Domain -> the one tool that may act on it. A domain absent here has no write path at all.
DOMAIN_TOOLS: Final[Mapping[str, str]] = {
    "light": "home.light",
    "switch": "home.switch",
    "scene": "home.scene",
    "media_player": "home.media",
    "climate": "home.climate",
    "cover": "home.cover",
    "script": "home.script",
    "automation": "home.automation.trigger",
}

#: Domains with a write path (the keys of :data:`DOMAIN_TOOLS`).
CONTROLLABLE_DOMAINS: Final[frozenset[str]] = frozenset(DOMAIN_TOOLS)

#: Domains Nox may read but never act on: measurements and the weather.
READABLE_DOMAINS: Final[frozenset[str]] = frozenset({"sensor", "binary_sensor", "weather"})


class ForbiddenEntityError(PermissionError):
    """An entity on the wrong side of the boundary was named explicitly.

    A `PermissionError` rather than a `ValueError` so that a caller which turns exceptions into
    user-facing text cannot mistake it for a typo: the entity exists, and Nox refuses it.
    """

    def __init__(self, entity_id: str, reason: str) -> None:
        super().__init__(f"{entity_id}: {reason}")
        self.entity_id = entity_id
        self.reason = reason


def domain_of(entity_id: str) -> str:
    """`"light.kitchen"` -> `"light"`. An id without a dot has no domain and yields `""`."""
    domain, separator, _ = entity_id.partition(".")
    return domain if separator else ""


def _device_class(attributes: Mapping[str, Any] | None) -> str:
    if not attributes:
        return ""
    value = attributes.get("device_class")
    return str(value).strip().lower() if value is not None else ""


def forbidden_reason(entity_id: str, attributes: Mapping[str, Any] | None = None) -> str | None:
    """Why this entity is off limits, or `None` when it is not.

    The string is a developer-facing explanation for the log and the tool error; the dashboard
    never shows it, because a forbidden entity never reaches the dashboard in the first place.
    """
    domain = domain_of(entity_id)
    if domain in FORBIDDEN_DOMAINS:
        return (
            f"the {domain!r} domain secures the building and is never exposed to Nox "
            "(hard boundary, see docs/PRIVACY.md)"
        )
    if domain == "cover":
        device_class = _device_class(attributes)
        if device_class in FORBIDDEN_COVER_DEVICE_CLASSES:
            return (
                f"a cover with device_class {device_class!r} is an entrance, not a blind, "
                "and is never exposed to Nox (hard boundary)"
            )
    if domain in PRIVATE_DOMAINS:
        return f"the {domain!r} domain describes people rather than devices and stays private"
    return None


def is_exposed(entity_id: str, attributes: Mapping[str, Any] | None = None) -> bool:
    """Whether this entity may appear in a listing at all."""
    if forbidden_reason(entity_id, attributes) is not None:
        return False
    domain = domain_of(entity_id)
    return domain in CONTROLLABLE_DOMAINS or domain in READABLE_DOMAINS


def require_allowed(
    entity_id: str,
    *,
    expected_domain: str | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> str:
    """Check one entity id before a service call is built; returns its domain.

    Raises :class:`ForbiddenEntityError` for anything on the wrong side of the boundary and
    `ValueError` for a mismatch the caller can fix (wrong tool for the domain, malformed id).
    """
    domain = domain_of(entity_id)
    if not domain:
        raise ValueError(f"{entity_id!r} is not a Home Assistant entity id (expected 'domain.id')")
    reason = forbidden_reason(entity_id, attributes)
    if reason is not None:
        raise ForbiddenEntityError(entity_id, reason)
    if expected_domain is not None and domain != expected_domain:
        tool = DOMAIN_TOOLS.get(domain)
        hint = f"; use {tool} for it" if tool else ""
        raise ValueError(f"{entity_id!r} is a {domain!r} entity, not {expected_domain!r}{hint}")
    if expected_domain is None and domain not in CONTROLLABLE_DOMAINS:
        raise ValueError(f"{entity_id!r} is in the {domain!r} domain, which Nox cannot control")
    return domain


# ---- effect: what a call really touches ----------------------------------------------------------


class Effect(StrEnum):
    """How far a call may go, judged by the entities it would really change."""

    SAFE = "safe"
    CONFIRM = "confirm"
    FORBIDDEN = "forbidden"


_EFFECT_RANK: Final[dict[Effect, int]] = {Effect.SAFE: 0, Effect.CONFIRM: 1, Effect.FORBIDDEN: 2}


@dataclass(frozen=True, slots=True)
class EffectFinding:
    """The strictest effect found, the entities that caused it and a sentence saying why."""

    effect: Effect = Effect.SAFE
    entities: tuple[str, ...] = ()
    reason: str = ""

    def merge(self, other: EffectFinding) -> EffectFinding:
        """The stricter of two findings; two findings of the same strictness pool their entities."""
        if _EFFECT_RANK[other.effect] > _EFFECT_RANK[self.effect]:
            return other
        if other.effect is self.effect and other.effect is not Effect.SAFE:
            entities = tuple(dict.fromkeys((*self.entities, *other.entities)))
            return EffectFinding(self.effect, entities, self.reason or other.reason)
        return self


def looks_like_entrance(
    entity_id: str, name: str = "", attributes: Mapping[str, Any] | None = None
) -> bool:
    """Whether a switch probably drives a garage door, a gate or a door opener.

    Its device class says so, or its id or friendly name does (:data:`ENTRANCE_NAME_FRAGMENTS`).
    """
    if _device_class(attributes) in ENTRANCE_DEVICE_CLASSES:
        return True
    text = f"{entity_id} {name}".lower().replace("_", " ").replace(".", " ")
    for harmless in _NOT_AN_ENTRANCE:
        text = text.replace(harmless, " ")
    if any(fragment in text for fragment in ENTRANCE_NAME_FRAGMENTS):
        return True
    return _TOR_RE.search(text) is not None


def entity_effect(
    entity_id: str, attributes: Mapping[str, Any] | None = None, name: str = ""
) -> EffectFinding:
    """The effect of changing one entity's state."""
    reason = forbidden_reason(entity_id, attributes)
    if reason is not None:
        return EffectFinding(Effect.FORBIDDEN, (entity_id,), reason)
    domain = domain_of(entity_id)
    if domain == "switch" and looks_like_entrance(entity_id, name, attributes):
        return EffectFinding(
            Effect.CONFIRM, (entity_id,), "this switch looks like it opens a garage, gate or door"
        )
    if domain == "cover" and _device_class(attributes) not in SAFE_COVER_DEVICE_CLASSES:
        return EffectFinding(
            Effect.CONFIRM,
            (entity_id,),
            "this cover does not report itself as a blind, shutter, shade, curtain or awning",
        )
    return EffectFinding(Effect.SAFE)


def members_effect(
    members: Iterable[str],
    attributes_of: Mapping[str, Mapping[str, Any]],
    names_of: Mapping[str, str] | None = None,
) -> EffectFinding:
    """The effect of activating a scene that sets `members`."""
    names = names_of or {}
    finding = EffectFinding()
    for entity_id in members:
        finding = finding.merge(
            entity_effect(entity_id, attributes_of.get(entity_id), names.get(entity_id, ""))
        )
    return finding


@dataclass(frozen=True, slots=True)
class ConfigReferences:
    """What a script or automation definition names: entities, service domains, and the targets
    that could not be resolved to entities (an area, a device, a template)."""

    entities: tuple[str, ...] = ()
    service_domains: frozenset[str] = field(default_factory=frozenset)
    unresolved: tuple[str, ...] = ()


#: Keys that name a service call; `action` is the newer spelling of `service`.
_SERVICE_KEYS: Final[tuple[str, ...]] = ("service", "action")
#: Keys that target something bigger than one entity.
_GROUP_TARGET_KEYS: Final[tuple[str, ...]] = ("area_id", "floor_id", "label_id")


class _ReferenceCollector:
    """Walks one definition; see :func:`config_references`."""

    def __init__(self) -> None:
        self.entities: dict[str, None] = {}
        self.domains: set[str] = set()
        self.unresolved: dict[str, None] = {}

    def walk(self, node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                self.walk(item)
            return
        if not isinstance(node, Mapping):
            return
        for key in _SERVICE_KEYS:
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                self._service(value.strip())
        if "device_id" in node:
            self._device(node)
        for key in _GROUP_TARGET_KEYS:
            if key in node:
                self.unresolved.setdefault(key.removesuffix("_id"), None)
        if "entity_id" in node:
            self._entities(node["entity_id"])
        for key, value in node.items():
            if key != "entity_id":
                self.walk(value)

    def _service(self, text: str) -> None:
        if "{" in text or not domain_of(text):
            self.unresolved.setdefault(f"service {text}", None)
        else:
            self.domains.add(domain_of(text))

    def _device(self, node: Mapping[str, Any]) -> None:
        device_domain = str(node.get("domain") or "").strip()
        if device_domain:
            self.domains.add(device_domain)
        else:
            self.unresolved.setdefault("device", None)

    def _entities(self, value: Any) -> None:
        for item in value if isinstance(value, list) else [value]:
            text = str(item).strip()
            if "{" in text or not domain_of(text):
                self.unresolved.setdefault(f"entity {text}", None)
            else:
                self.entities.setdefault(text, None)


def config_references(config: Any) -> ConfigReferences:
    """Collect everything a script or automation definition would act on.

    Deliberately over-inclusive: an entity mentioned in a condition counts as well, because telling
    a condition from an action in every Home Assistant syntax is not worth the risk of getting it
    wrong. Anything that is not a literal entity id - a template, an area, a device without a
    domain - is reported as unresolved.
    """
    collector = _ReferenceCollector()
    collector.walk(config)
    return ConfigReferences(
        entities=tuple(collector.entities),
        service_domains=frozenset(collector.domains),
        unresolved=tuple(collector.unresolved),
    )


#: Domains whose services start something that has its own definition (another script, a scene).
_INDIRECT_DOMAINS: Final[frozenset[str]] = frozenset({"script", "scene", "automation"})


def config_effect(
    references: ConfigReferences,
    attributes_of: Mapping[str, Mapping[str, Any]],
    names_of: Mapping[str, str] | None = None,
) -> EffectFinding:
    """The effect of running a script or automation whose definition names `references`.

    A service in a forbidden domain (`lock.unlock`, `alarm_control_panel.alarm_disarm`,
    `valve.open_valve`) is forbidden, and so is any forbidden entity. Everything that cannot be
    pinned down - an area, a device without a domain, a template, another script or scene started
    from inside this one - needs a confirmation.
    """
    finding = EffectFinding()
    for domain in sorted(references.service_domains & FORBIDDEN_DOMAINS):
        finding = finding.merge(
            EffectFinding(
                Effect.FORBIDDEN,
                (f"{domain}.*",),
                f"it calls a {domain!r} service, which secures the building (hard boundary)",
            )
        )
    finding = finding.merge(members_effect(references.entities, attributes_of, names_of))
    indirect = sorted(references.service_domains & _INDIRECT_DOMAINS)
    if references.unresolved or indirect:
        finding = finding.merge(
            EffectFinding(
                Effect.CONFIRM,
                (*references.unresolved, *(f"{domain}.*" for domain in indirect)),
                "part of what it changes cannot be resolved to single entities",
            )
        )
    return finding

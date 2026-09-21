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

Both halves of the feature import this module - the `home` plugin worker that talks to Home
Assistant and the core-side intent layer - so the two can never drift apart.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

__all__ = [
    "CONTROLLABLE_DOMAINS",
    "DOMAIN_TOOLS",
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

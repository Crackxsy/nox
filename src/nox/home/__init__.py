"""Smart-home control through the user's own Home Assistant instance.

This package owns the core-side half of the feature: the domain boundary both the plugin and the
core agree on (`nox.home.boundary`), the deterministic German/English intent layer that answers
"mach das licht im wohnzimmer aus" without an AI round trip (`nox.home.intent`), the connection
probe the Settings page's "Verbindung testen" button uses (`nox.home.probe`), and the `home.*` IPC
requests the dashboard calls (`nox.home.ipc`). The actual Home Assistant connection lives in the
`home` plugin worker (`plugins/home/`), like every other external transport in Nox.

It deliberately owns no device drivers, no vendor SDK and no cloud account: everything goes to one
Home Assistant instance on the user's own network, and nothing here ever reaches a manufacturer
cloud.
"""

from __future__ import annotations

from nox.home.boundary import (
    CONTROLLABLE_DOMAINS,
    FORBIDDEN_COVER_DEVICE_CLASSES,
    FORBIDDEN_DOMAINS,
    READABLE_DOMAINS,
    ForbiddenEntityError,
    domain_of,
    forbidden_reason,
    is_exposed,
    require_allowed,
)
from nox.home.intent import Intent, IntentMatch, IntentRefusal, resolve_intent
from nox.home.probe import ProbeResult, probe_home_assistant
from nox.home.targets import IntentSnapshot, KnownEntity

__all__ = [
    "CONTROLLABLE_DOMAINS",
    "FORBIDDEN_COVER_DEVICE_CLASSES",
    "FORBIDDEN_DOMAINS",
    "READABLE_DOMAINS",
    "ForbiddenEntityError",
    "Intent",
    "IntentMatch",
    "IntentRefusal",
    "IntentSnapshot",
    "KnownEntity",
    "ProbeResult",
    "domain_of",
    "forbidden_reason",
    "is_exposed",
    "probe_home_assistant",
    "require_allowed",
    "resolve_intent",
]

"""What one plugin connection may hear and say on the hub, enforced by the core.

A plugin's manifest names the events it listens to and the events it emits. The plugin API checks
both inside the plugin process, but code in a plugin - or a compromised dependency of one - can
skip that API and talk to the socket directly. So the hub applies the same manifest a second time,
on its own side of the connection:

* **Listening.** A subscription pattern is narrowed to the manifest's `events.listens` plus the few
  lifecycle events every plugin worker needs (:data:`PLUGIN_BASE_LISTENS`). `**` from a plugin
  becomes exactly that list, never "everything". Every event is checked again on delivery.
* **Emitting.** Only the exact names in `events.emits`, and never a name in one of the
  :data:`RESERVED_EVENT_NAMESPACES` - the namespaces the core itself speaks in. A forged
  `privacy.capture_changed` or `system.started` from a plugin would otherwise re-open the
  microphone or clear safe mode.

A plugin connection with no scope registered (the core did not spawn it) gets the lifecycle events
and may emit nothing: unknown is never "allowed".

This module deliberately knows nothing about manifests; `nox.plugins.manager` builds the scope from
a validated manifest and hands it to the hub before the worker process starts.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from nox.core.globbing import name_matches, name_matches_any

#: Event namespaces only the core publishes into. A manifest that declares an `emits` entry in one
#: of them fails validation, and the hub refuses such an event even if a manifest slipped through.
RESERVED_EVENT_NAMESPACES: frozenset[str] = frozenset(
    {
        "privacy",  # capture flags, zones, the privacy mode
        "security",  # kill switch, panic, permission requests and decisions
        "system",  # started/stopping/mode: `system.started` clears the orchestrator's safe mode
        "sup",  # the supervisor's control channel
        "worker",  # worker lifecycle
        "ipc",  # hub connection notices
        "plugin",  # plugin lifecycle, published by the plugin manager
        "voice",  # speech in and out, wake word, kill phrase
        "memory",  # what Nox remembers
    }
)

#: Events every plugin worker receives whatever its manifest says: the ones it needs to stop on
#: time and to know the privacy state its own egress guard enforces.
PLUGIN_BASE_LISTENS: tuple[str, ...] = (
    "security.kill_switch",
    "security.panic",
    "privacy.mode_changed",
    "privacy.capture_changed",
    "system.stopping",
)


def is_reserved_event(name: str) -> bool:
    """Whether `name` lies in a namespace only the core may publish into."""
    return name.split(".", 1)[0] in RESERVED_EVENT_NAMESPACES


@dataclass(frozen=True)
class PluginScope:
    """The listen/emit boundary of one plugin connection (see the module docstring)."""

    listens: tuple[str, ...] = ()
    emits: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def of(cls, *, listens: Iterable[str], emits: Iterable[str]) -> PluginScope:
        return cls(listens=tuple(dict.fromkeys(listens)), emits=frozenset(emits))

    @property
    def allowed_listens(self) -> tuple[str, ...]:
        """Every name or pattern this plugin may receive."""
        return tuple(dict.fromkeys((*PLUGIN_BASE_LISTENS, *self.listens)))

    def narrow(self, pattern: str) -> list[str]:
        """The allowed listen entries a requested subscription `pattern` covers.

        An exact manifest entry passes as itself. A broader request (`security.*`, `**`) is reduced
        to the plain names it covers. A wildcard entry in the manifest is only granted when it is
        requested verbatim, because deciding whether one glob contains another is not worth the
        risk of getting it wrong. An empty result means the request covers nothing allowed.
        """
        covered: list[str] = []
        for allowed in self.allowed_listens:
            if allowed == pattern or ("*" not in allowed and name_matches(allowed, pattern)):
                covered.append(allowed)
        return covered

    def may_receive(self, name: str) -> bool:
        return name_matches_any(name, self.allowed_listens)

    def may_emit(self, name: str) -> bool:
        return name in self.emits and not is_reserved_event(name)


#: The scope of a plugin connection the core did not register: lifecycle events only, no emits.
NO_SCOPE = PluginScope()

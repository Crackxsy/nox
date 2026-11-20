"""The satellites on the core, wired: voice turns, overlay credentials, the microphone gate, panic.

`install_satellites` builds the `SatelliteHub` (`nox.node.hub.satellites`) from the registry, the
listener, the pairing and the core's security and assistant, hangs it onto the core through the
core's extension points (a node overlay check, a satellite label, bus subscriptions, a removal and
a narrowing hook), and hands back the two connection hooks the caller adds to the listener - in the
order that matters there (`nox.node.hub.install`). It owns the questions the hub asks about nodes;
the hub's own rules are in `nox.node.hub.satellites`.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from nox.core.events import E, Event
from nox.node.hub.listener import NodeListener, NodeSession, SessionFn
from nox.node.hub.pairing import NodePairing
from nox.node.hub.policy import group_of
from nox.node.hub.ports import CoreServices
from nox.node.hub.registry import NodeEntry, NodeRegistry
from nox.node.hub.satellites import (
    NodeDirectory,
    OverlayAccess,
    SatelliteHub,
    VoiceAssistant,
    clean_label,
)
from nox.security._events import publish_nowait

__all__ = ["OVERLAY_CHECK_TTL_S", "SatelliteWiring", "TtlCache", "install_satellites"]

#: How long the hub may reuse its answer to "may this node hold an overlay credential?".
OVERLAY_CHECK_TTL_S = 2.0
#: The name under which the node extension registers on the core's extension points.
HOOK_NAME = "nodes"


class TtlCache:
    """`read(key)` remembered for `ttl_s`; `forget(key)` drops one answer at once."""

    def __init__(
        self,
        read: Callable[[str], bool],
        *,
        ttl_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read, self._ttl, self._clock = read, ttl_s, clock
        self._answers: dict[str, tuple[float, bool]] = {}

    def __call__(self, key: str) -> bool:
        now = self._clock()
        cached = self._answers.get(key)
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]
        value = bool(self._read(key))
        self._answers[key] = (now, value)
        return value

    def forget(self, key: str) -> None:
        self._answers.pop(key, None)


@dataclass(frozen=True)
class SatelliteWiring:
    """The hub, and what the caller adds to the listener's connection hooks."""

    hub: SatelliteHub
    #: After a node connected and was listed: its gate goes out, and the panic if one is on.
    on_connected: SessionFn
    #: After a node's connection ended, before the registry marks it offline.
    on_disconnected: SessionFn


class _Directory:
    """What the registry knows about each node, as the hub asks for it."""

    def __init__(self, registry: NodeRegistry) -> None:
        self._registry = registry

    def entry_of(self, device_id: str) -> NodeEntry | None:
        return next((e for e in self._registry.known() if e.device_id == device_id), None)

    def offers(self, device_id: str, tool: str) -> bool:
        entry = self.entry_of(device_id)
        return entry is not None and entry.offers(tool)

    def enabled_on_device(self, device_id: str, group: str) -> bool:
        entry = self.entry_of(device_id)
        if entry is None or not entry.online:
            return False
        if any(group_of(tool) == group for tool in entry.tools):
            return True
        return group in entry.narrowed and self._registry.policy.state(device_id, group)[0]

    def name_of(self, device_id: str) -> str:
        """The node's name as it may be shown, or "" for a device this registry does not know -
        so that another extension's label provider gets to name it."""
        entry = self.entry_of(device_id)
        return clean_label(entry.label if entry is not None else "")

    def label_of(self, device_id: str) -> str:
        return self.name_of(device_id) or device_id[:8]


def install_satellites(
    services: CoreServices, registry: NodeRegistry, listener: NodeListener, pairing: NodePairing
) -> SatelliteWiring:
    """The core's side of the satellites (`nox.node.hub.satellites`): voice turns from nodes,
    their speech levels for a pet, overlay credentials for their overlay relays, the microphone
    gate (pushed on connection, every second, and at once on a privacy change), and the panic
    button for OBS on nodes."""
    security = services.security
    orchestrator = services.orchestrator
    directory = _Directory(registry)

    def capture_refusal() -> str | None:
        if security.killswitch.is_engaged():
            return "stopped"
        try:
            allowed = bool(security.privacy.allows_capture("microphone"))
        except Exception:  # noqa: BLE001 - cannot tell: closed
            return "unknown"
        return None if allowed else "microphone off (privacy settings, panic or safe mode)"

    async def turn(
        text: str,
        language: str,
        on_chunk: Callable[[str], Awaitable[None]],
        channel: str,
        note: str,
    ) -> object:
        try:
            during_session = bool(hub.session_active())
        except Exception:  # noqa: BLE001 - cannot tell: as if one ran
            during_session = True
        if during_session:
            # While Nox controls a paired PC, the same rule as for paired-device chat:
            # the conversation holds unreadable untrusted content.
            security.engine.mark_tainted(orchestrator.conversation_for(channel), None)
        # Waits for the turn in flight instead of cancelling it (and a question it may wait on),
        # and interrupts no speech here; the conversation counts as tainted from its first turn
        # (`nox.security.taint.UNTRUSTED_CONVERSATIONS`).
        return await orchestrator.handle_text(
            text,
            language=language,
            speak=False,
            on_chunk=on_chunk,
            channel=channel,
            barge_in=False,
            context_note=note,
        )

    def publish(name: str, payload: dict[str, Any]) -> None:
        publish_nowait(services.bus, name, payload, source="node")

    def read_overlay_allowed(device_id: str) -> bool:
        """May this node hold an overlay credential (blocking: the database)? Paired, not
        removed, `obs` enabled on the device as it last said and not narrowed here."""
        device = pairing.get(device_id)
        if device is None or device.revoked:
            return False
        on_device, narrowed = registry.policy.state(device_id, "obs")
        return on_device and not narrowed

    # The hub asks at every use of a node's overlay credential, on its own loop: a short cache,
    # emptied for a node the moment it is narrowed or removed.
    overlay_allowed = TtlCache(read_overlay_allowed, ttl_s=OVERLAY_CHECK_TTL_S)
    # The hub asks again whenever a node's overlay credential is used over the network.
    services.tokens.add_node_overlay_check(HOOK_NAME, overlay_allowed)
    hub = SatelliteHub(
        NodeDirectory(
            offers=directory.offers,
            enabled_on_device=directory.enabled_on_device,
            sessions=lambda: list(listener.sessions.values()),
            opted_in=registry.policy.opted_in,
            label=directory.label_of,
        ),
        VoiceAssistant(turn=turn, capture_refusal=capture_refusal, publish=publish),
        OverlayAccess(
            store=services.tokens.overlay,
            disconnect=services.hub.disconnect_credential,
            allowed=overlay_allowed,
        ),
        audit=security.audit,
    )
    listener.on_satellite = hub.handle
    # "von Sprach-Satellit <name>" in its questions
    security.engine.add_satellite_label(HOOK_NAME, directory.name_of)

    def panicking() -> bool:
        try:
            return bool(security.privacy.panic)
        except Exception:  # noqa: BLE001 - cannot tell: protective
            return True

    async def on_connected(session: NodeSession) -> None:
        await hub.push_gates()
        if panicking():
            # Offline when the panic button was pressed: its OBS goes to the privacy scene now.
            await hub.panic("panic", sessions=[session])

    async def on_disconnected(session: NodeSession) -> None:
        hub.disconnected(session.device_id)

    async def forget(device_id: str, reason: str) -> None:
        overlay_allowed.forget(device_id)
        await hub.revoke_overlay(device_id, f"node removed: {reason}")

    pairing.on_revoked(forget)

    async def narrowed(device_id: str, group: str, on: bool) -> None:
        if group == "obs":
            overlay_allowed.forget(device_id)
        if group == "obs" and on:
            # Switched off in the Devices page: its overlay goes dark now, online or not.
            await hub.revoke_overlay(device_id, "obs narrowed")

    registry.on_narrowed.append(narrowed)

    async def gates(_event: Event) -> None:
        await hub.push_gates()

    async def panic(_event: Event) -> None:
        await hub.push_gates()
        await hub.panic("panic")

    for name in (E.PRIVACY_CAPTURE_CHANGED, E.SECURITY_KILL_SWITCH, E.SYSTEM_STARTED):
        services.bus.subscribe(name, gates)
    services.bus.subscribe(E.SECURITY_PANIC, panic)
    return SatelliteWiring(hub, on_connected, on_disconnected)

"""Outbound notifications to the paired phone.

Three gates, in this order, and the first one that says no wins:

1. **Allow-list.** Only the event names in `remote.notifications.events` are considered at all. An
event that is not named there is never forwarded - a denylist would silently start leaking the next
event someone adds to `E`. 2. **Privacy zones.** While a privacy zone is active nothing but a
critical event leaves the machine, and the text is built from *metadata only* (event name plus a
handful of allow-listed scalar fields). Zone content is never described, quoted or summarised - the
zone check runs before any text is composed, mirroring the zone-gate-before-draft ordering
requires. 3. **Quiet hours.** The same `attention.quiet_hours` window the local `SpeechPolicy` uses
(23:00-08:00 by default); critical events (the kill switch) still pass, everything else is
suppressed and counted as `quiet_hours`.

`security.audit`, `voice.transcript_*`, `ai.*` and `memory.*` can never be configured into this
path: `_render` has no formatter for them, so they would produce no text even if allow-listed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any

from nox.core.config import QuietHoursConfig
from nox.core.events import E, Event, EventBus
from nox.core.logging import get_logger
from nox.core.speech_policy import in_quiet_hours

log = get_logger(__name__)

#: Events that pass the quiet-hours gate anyway (PRD: URGENT still gets through).
CRITICAL_EVENTS: frozenset[str] = frozenset({E.SECURITY_KILL_SWITCH, E.SECURITY_PANIC})

Notify = Callable[[str], Awaitable[None]]


def _render(name: str, payload: dict[str, Any]) -> str:
    """Metadata-only rendering. Every formatter reads named scalar fields; none passes free text
    through, so an event payload can never carry content onto the phone by accident."""
    if name == E.SECURITY_KILL_SWITCH:
        # `nox.core.events.KillSwitch` is `{by, reason}`; `reason` is a short operator string
        # ("hotkey", "kill switch from paired phone"), never user content.
        return f"Not-Aus aktiv (ausgelöst von {payload.get('by', 'unbekannt')})."
    if name == E.SECURITY_PANIC:
        return "Panikmodus aktiv."
    if name == E.SYSTEM_HEALTH_CHANGED:
        return f"Komponente {payload.get('component', '?')}: {payload.get('status', 'unbekannt')}."
    if name == E.STREAM_STARTED:
        return "Stream gestartet."
    if name == E.STREAM_ENDED:
        return "Stream beendet."
    if name == "pm.focus_changed":
        return f"Fokus: {payload.get('focus', 'unbekannt')}."
    return ""


class RemoteNotifier:
    def __init__(
        self,
        *,
        bus: EventBus,
        send: Notify,
        events: Sequence[str],
        quiet_hours: QuietHoursConfig,
        active_zone: Callable[[], str | None],
        has_device: Callable[[], bool],
        enabled: bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._bus = bus
        self._send = send
        self._events = tuple(events)
        self._quiet_hours = quiet_hours
        self._active_zone = active_zone
        self._has_device = has_device
        self._enabled = enabled
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._unsubs: list[Callable[[], None]] = []

    def start(self) -> None:
        if not self._enabled or self._unsubs:
            return
        self._unsubs = [self._bus.subscribe(name, self._on_event) for name in self._events]

    def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    def gate(self, name: str) -> tuple[bool, str]:
        """`(allowed, reason)` for one event name, without sending anything."""
        if not self._enabled:
            return False, "disabled"
        if name not in self._events:
            return False, "not_allow_listed"
        if not self._has_device():
            return False, "no_device"
        critical = name in CRITICAL_EVENTS
        if self._active_zone() is not None and not critical:
            return False, "privacy_zone"
        if in_quiet_hours(self._quiet_hours, self._clock().time()) and not critical:
            return False, "quiet_hours"
        return True, "ok"

    async def _on_event(self, event: Event) -> None:
        allowed, reason = self.gate(event.name)
        text = _render(event.name, dict(event.payload)) if allowed else ""
        if allowed and not text:
            allowed, reason = False, "no_formatter"
        if allowed:
            try:
                await self._send(text)
            except Exception as exc:  # noqa: BLE001 - a failed send never breaks the bus
                log.warning("remote.notify_failed", name=event.name, error=type(exc).__name__)
                allowed, reason = False, "send_failed"
        log.debug("remote.notification", name=event.name, sent=allowed, reason=reason)
        await self._bus.publish(
            Event(
                name=E.REMOTE_NOTIFICATION_SENT,
                payload={"event": event.name, "sent": allowed, "reason": reason},
            )
        )

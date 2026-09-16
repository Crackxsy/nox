"""`RemoteNotifier` gating (ST-17-06, Spec v0.8 §7, FR-12.4/FR-12.5).

The three gates are tested as their own properties rather than as a side effect of a happy path:
an event outside the allow-list can never be sent, a zone-active moment suppresses everything
non-critical *before* any text is composed, and quiet hours suppress everything except the kill
switch. The metadata-only rule gets its own test: a payload field that looks like content must not
appear in what is sent.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nox.core.config import QuietHoursConfig
from nox.core.events import E, Event
from nox.remote.notify import RemoteNotifier

from .conftest import Clock

DAYTIME = datetime(2026, 9, 14, 14, 0, tzinfo=UTC)
NIGHT = datetime(2026, 9, 14, 23, 30, tzinfo=UTC)

ALLOWED = [E.SECURITY_KILL_SWITCH, E.SYSTEM_HEALTH_CHANGED, E.STREAM_STARTED, E.STREAM_ENDED]


class Zone:
    def __init__(self, active: str | None = None) -> None:
        self.active = active

    def __call__(self) -> str | None:
        return self.active


def make_notifier(
    bus,
    *,
    events=None,
    now: datetime = DAYTIME,
    zone: Zone | None = None,
    has_device: bool = True,
    enabled: bool = True,
):
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    notifier = RemoteNotifier(
        bus=bus,
        send=send,
        events=ALLOWED if events is None else events,
        quiet_hours=QuietHoursConfig(start="23:00", end="08:00"),
        active_zone=zone or Zone(),
        has_device=lambda: has_device,
        enabled=enabled,
        clock=Clock(now),
    )
    return notifier, sent


async def test_an_allow_listed_event_is_forwarded_as_metadata(bus):
    notifier, sent = make_notifier(bus)
    notifier.start()

    await bus.publish(
        Event(name=E.STREAM_STARTED, payload={"session_id": "abc", "title": "geheimer Titel"})
    )

    assert sent == ["Stream gestartet."]
    assert "geheimer Titel" not in sent[0]  # metadata only: no payload text is passed through


async def test_an_event_outside_the_allow_list_is_never_sent(bus):
    notifier, sent = make_notifier(bus, events=[E.SECURITY_KILL_SWITCH])
    notifier.start()

    await bus.publish(Event(name=E.STREAM_STARTED, payload={}))

    assert sent == []
    assert notifier.gate(E.STREAM_STARTED) == (False, "not_allow_listed")


@pytest.mark.parametrize(
    "name",
    ["voice.transcript_ready", "ai.response_ready", "memory.created", "security.audit"],
)
async def test_privacy_sensitive_events_have_no_formatter_even_if_allow_listed(bus, name):
    """Belt and braces for IPC Model "Outbound filtering": even a misconfigured allow-list cannot
    push a transcript, an AI response, a memory entry or an audit detail to the phone."""
    notifier, sent = make_notifier(bus, events=[name])
    notifier.start()

    await bus.publish(Event(name=name, payload={"text": "geheim"}))

    assert sent == []
    published = [e for e in bus.published if e.name == E.REMOTE_NOTIFICATION_SENT]
    assert published[-1].payload["reason"] == "no_formatter"


async def test_an_active_privacy_zone_suppresses_non_critical_events(bus):
    notifier, sent = make_notifier(bus, zone=Zone("discord"))
    notifier.start()

    await bus.publish(Event(name=E.STREAM_STARTED, payload={}))

    assert sent == []
    assert notifier.gate(E.STREAM_STARTED) == (False, "privacy_zone")


async def test_the_kill_switch_passes_an_active_zone(bus):
    notifier, sent = make_notifier(bus, zone=Zone("discord"))
    notifier.start()

    await bus.publish(Event(name=E.SECURITY_KILL_SWITCH, payload={"by": "ui"}))

    assert sent == ["Not-Aus aktiv (ausgelöst von ui)."]


async def test_quiet_hours_suppress_non_critical_events(bus):
    notifier, sent = make_notifier(bus, now=NIGHT)
    notifier.start()

    await bus.publish(Event(name=E.SYSTEM_HEALTH_CHANGED, payload={"component": "tts"}))

    assert sent == []
    assert notifier.gate(E.SYSTEM_HEALTH_CHANGED) == (False, "quiet_hours")


async def test_quiet_hours_still_let_the_kill_switch_through(bus):
    notifier, sent = make_notifier(bus, now=NIGHT)
    notifier.start()

    await bus.publish(Event(name=E.SECURITY_KILL_SWITCH, payload={"by": "ui"}))

    assert len(sent) == 1


async def test_nothing_is_sent_without_a_paired_device(bus):
    notifier, sent = make_notifier(bus, has_device=False)
    notifier.start()

    await bus.publish(Event(name=E.SECURITY_KILL_SWITCH, payload={"by": "ui"}))

    assert sent == []
    assert notifier.gate(E.SECURITY_KILL_SWITCH) == (False, "no_device")


async def test_a_disabled_notifier_subscribes_to_nothing(bus):
    notifier, sent = make_notifier(bus, enabled=False)
    notifier.start()

    await bus.publish(Event(name=E.SECURITY_KILL_SWITCH, payload={"by": "ui"}))

    assert bus.subs == [] and sent == []


async def test_every_decision_is_reported_as_an_event(bus):
    notifier, _ = make_notifier(bus, zone=Zone("discord"))
    notifier.start()

    await bus.publish(Event(name=E.STREAM_ENDED, payload={}))

    reported = [e for e in bus.published if e.name == E.REMOTE_NOTIFICATION_SENT]
    assert reported[-1].payload == {
        "event": E.STREAM_ENDED,
        "sent": False,
        "reason": "privacy_zone",
    }


async def test_stop_unsubscribes(bus):
    notifier, sent = make_notifier(bus)
    notifier.start()
    notifier.stop()

    await bus.publish(Event(name=E.STREAM_STARTED, payload={}))

    assert sent == []

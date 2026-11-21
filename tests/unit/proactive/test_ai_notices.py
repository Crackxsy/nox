"""The AI layer's two events become one notification each, said once and not on every turn."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from nox.core.config import NoxConfig
from nox.core.events import E, Event
from nox.proactive.ai_notices import FAILURE_NOTICE_COOLDOWN_S, AiNoticeRelay
from tests.unit.fakes import FakeBus
from tests.unit.proactive.test_service import FakeSpeaker, make_service

NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)


class FakeService:
    """The one method of `ProactiveService` the relay uses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    async def notify(self, kind: str, priority: str, text: str, *, context_key: str = "") -> None:
        self.calls.append((kind, priority, text, context_key))


class Time:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def service() -> FakeService:
    return FakeService()


@pytest.fixture
def clock() -> Time:
    return Time()


@pytest.fixture
def bus(service: FakeService, clock: Time) -> FakeBus:
    bus = FakeBus()
    relay = AiNoticeRelay(
        service,  # type: ignore[arg-type]
        language=lambda: "de",
        clock=lambda: NOW,
        monotonic=clock,
    )
    relay.attach(bus)  # type: ignore[arg-type]
    return bus


def limited(until: datetime, **extra: Any) -> Event:
    payload = {
        "provider": "claude_code",
        "kind": "five_hour",
        "until": until.isoformat(),
        "estimated": False,
        "fallback": "local",
        **extra,
    }
    return Event(name=E.AI_PROVIDER_LIMITED, payload=payload)


def failed(cause: str, **extra: Any) -> Event:
    return Event(
        name=E.AI_TURN_FAILED,
        payload={"cause": cause, "language": "de", "until": "", "estimated": False, **extra},
    )


async def test_a_limit_becomes_one_visible_warning_in_plain_words(
    bus: FakeBus, service: FakeService
) -> None:
    await bus.publish(limited(NOW + timedelta(hours=2)))

    ((kind, priority, text, key),) = service.calls
    assert (kind, priority, key) == ("urgent", "resources", "ai.limit:claude_code")
    assert text.startswith("Mein Claude-Kontingent ist bis ") and "lokale Modell" in text


async def test_the_same_limit_is_said_once(bus: FakeBus, service: FakeService) -> None:
    until = NOW + timedelta(hours=2)

    await bus.publish(limited(until))
    await bus.publish(limited(until))

    assert len(service.calls) == 1


async def test_a_new_limit_after_the_old_one_is_said_again(
    bus: FakeBus, service: FakeService
) -> None:
    await bus.publish(limited(NOW + timedelta(hours=2)))
    await bus.publish(limited(NOW + timedelta(days=5)))

    assert len(service.calls) == 2


async def test_a_failure_is_said_once_per_cooldown_not_after_every_sentence(
    bus: FakeBus, service: FakeService, clock: Time
) -> None:
    await bus.publish(failed("failed"))
    clock.now += 10
    await bus.publish(failed("failed"))
    clock.now += FAILURE_NOTICE_COOLDOWN_S
    await bus.publish(failed("failed"))

    assert len(service.calls) == 2
    assert all("antwortet gerade nicht" in call[2] for call in service.calls)


async def test_different_failures_do_not_silence_each_other(
    bus: FakeBus, service: FakeService
) -> None:
    await bus.publish(failed("failed"))
    await bus.publish(failed("unavailable"))

    assert [call[3] for call in service.calls] == ["ai.turn:failed", "ai.turn:unavailable"]


async def test_a_turn_stopped_by_an_announced_limit_does_not_say_it_a_second_time(
    bus: FakeBus, service: FakeService
) -> None:
    await bus.publish(limited(NOW + timedelta(hours=2)))
    await bus.publish(failed("limit", until=(NOW + timedelta(hours=2)).isoformat()))

    assert len(service.calls) == 1


async def test_a_limit_failure_is_said_when_no_limit_was_announced(
    bus: FakeBus, service: FakeService
) -> None:
    await bus.publish(failed("limit", until=(NOW + timedelta(hours=2)).isoformat()))

    assert len(service.calls) == 1 and "Kontingent" in service.calls[0][2]


async def test_a_limit_failure_after_the_announced_limit_ended_is_said_again(
    service: FakeService, clock: Time
) -> None:
    moments = [NOW]
    bus = FakeBus()
    AiNoticeRelay(
        service,  # type: ignore[arg-type]
        language=lambda: "de",
        clock=lambda: moments[0],
        monotonic=clock,
    ).attach(bus)  # type: ignore[arg-type]
    await bus.publish(limited(NOW + timedelta(hours=1)))
    moments[0] = NOW + timedelta(hours=2)
    clock.now += FAILURE_NOTICE_COOLDOWN_S

    await bus.publish(failed("limit", until=(NOW + timedelta(hours=1)).isoformat()))

    assert len(service.calls) == 2


@pytest.mark.parametrize(
    "event",
    [
        Event(name=E.AI_PROVIDER_LIMITED, payload={"provider": "claude_code"}),
        Event(name=E.AI_PROVIDER_LIMITED, payload={"provider": "p", "kind": "k", "until": "no"}),
        Event(name=E.AI_TURN_FAILED, payload={"cause": "never-heard-of"}),
        Event(name=E.AI_TURN_FAILED, payload={}),
    ],
)
async def test_a_malformed_event_says_nothing(
    bus: FakeBus, service: FakeService, event: Event
) -> None:
    await bus.publish(event)

    assert service.calls == []


async def test_after_detach_nothing_is_relayed(service: FakeService) -> None:
    bus = FakeBus()
    relay = AiNoticeRelay(service, language=lambda: "de")  # type: ignore[arg-type]
    relay.attach(bus)  # type: ignore[arg-type]
    relay.detach()

    await bus.publish(failed("failed"))

    assert service.calls == []


# ---- through the real service: who may hear it -------------------------------------------


async def relayed_through_the_service(
    **service_options: Any,
) -> tuple[list[str], list[tuple[str, dict[str, Any]]]]:
    speaker = FakeSpeaker()
    real, events, _ = make_service(speaker=speaker, **service_options)
    bus = FakeBus()
    AiNoticeRelay(real, language=lambda: "de", clock=lambda: NOW).attach(bus)  # type: ignore[arg-type]
    await bus.publish(limited(NOW + timedelta(hours=2)))
    return speaker.calls, events


async def test_the_limit_is_spoken_and_shown_when_speaking_is_allowed() -> None:
    spoken, events = await relayed_through_the_service()

    assert len(spoken) == 2 and spoken[1].startswith("Mein Claude-Kontingent")  # lead-in, then text
    assert [name for name, _ in events] == ["proactive.notification"]


@pytest.mark.parametrize(
    "options",
    [
        {"privacy_mode": "private"},
        {"privacy_mode": "offline"},
        {"muted": True},
        {"active_zone": "bedroom"},
        {"clock_hour": 3},
    ],
)
async def test_the_limit_is_shown_but_never_spoken_in_quiet_situations(
    options: dict[str, Any],
) -> None:
    spoken, events = await relayed_through_the_service(**options)

    assert spoken == []
    notices = [payload for name, payload in events if name == "proactive.notification"]
    assert len(notices) == 1 and "Kontingent" in notices[0]["text"]
    assert notices[0]["spoken"] is False


async def test_the_limit_is_not_spoken_in_a_mode_that_asks_for_no_interruptions() -> None:
    config = NoxConfig()
    config.attention.per_mode = {"focus": 0}
    speaker = FakeSpeaker()
    real, events, state = make_service(speaker=speaker, config=config)
    state.state.assistant.mode = "focus"  # type: ignore[assignment]
    bus = FakeBus()
    AiNoticeRelay(real, language=lambda: "de", clock=lambda: NOW).attach(bus)  # type: ignore[arg-type]

    await bus.publish(limited(NOW + timedelta(hours=2)))

    assert speaker.calls == []
    assert [name for name, _ in events] == ["proactive.notification"]

"""The AI layer's two events become one notification each, said once and not on every turn."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from nox.app import DEFAULTS_PATH
from nox.core.config import NoxConfig, load_config
from nox.core.events import E, Event
from nox.core.state import Mode
from nox.proactive.ai_notices import FAILURE_NOTICE_COOLDOWN_S, AiNoticeRelay
from tests.unit.fakes import FakeBus
from tests.unit.proactive.test_service import FakeSpeaker, make_service

NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)


class FakeService:
    """The one method of `ProactiveService` the relay uses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []
        self.speak: list[bool] = []

    async def notify(
        self, kind: str, priority: str, text: str, *, context_key: str = "", speak: bool = True
    ) -> None:
        self.calls.append((kind, priority, text, context_key))
        self.speak.append(speak)


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


async def test_a_limit_that_is_renewed_while_it_holds_is_the_same_limit(
    bus: FakeBus, service: FakeService
) -> None:
    """A limit without a reset time is locked for a quarter of an hour at a time, each time with
    another end. It is one limit and one announcement."""
    await bus.publish(limited(NOW + timedelta(minutes=15), estimated=True))
    await bus.publish(limited(NOW + timedelta(minutes=14), estimated=True))
    await bus.publish(limited(NOW + timedelta(days=5)))

    assert len(service.calls) == 1


async def test_a_new_limit_after_the_old_one_ended_is_said_again(
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
    await bus.publish(limited(NOW + timedelta(hours=2)))
    moments[0] = NOW + timedelta(hours=3)
    await bus.publish(limited(NOW + timedelta(days=5)))

    assert len(service.calls) == 2


async def test_a_failed_turn_is_only_shown_never_spoken_from_here(
    bus: FakeBus, service: FakeService
) -> None:
    """The orchestrator already said it, as the answer to what the user said."""
    await bus.publish(failed("unavailable"))

    assert service.speak == [False]


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


def shipped_config() -> NoxConfig:
    """The attention table as shipped, not as a test would like it."""
    return load_config(DEFAULTS_PATH, None, None, {})


async def relayed_through_the_service(
    event: Event, *, mode: Mode = Mode.COMPANION, **service_options: Any
) -> tuple[list[str], list[tuple[str, dict[str, Any]]]]:
    speaker = FakeSpeaker()
    config = service_options.pop("config", None) or shipped_config()
    real, events, state = make_service(speaker=speaker, config=config, **service_options)
    state.state.assistant.mode = mode
    bus = FakeBus()
    AiNoticeRelay(
        real,
        language=lambda: "de",
        mode=lambda: str(state.get("assistant.mode")),
        clock=lambda: NOW,
    ).attach(bus)  # type: ignore[arg-type]
    await bus.publish(event)
    return speaker.calls, events


async def test_the_limit_is_spoken_and_shown_when_speaking_is_allowed() -> None:
    spoken, events = await relayed_through_the_service(limited(NOW + timedelta(hours=2)))

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
        {"mode": Mode.ROCKET_LEAGUE},
        {"mode": Mode.FOCUS},
    ],
)
async def test_the_limit_is_shown_but_never_spoken_in_quiet_situations(
    options: dict[str, Any],
) -> None:
    """Including a running game (the shipped table lets `rocket_league` speak with ceiling 1) and
    focus (ceiling 0)."""
    spoken, events = await relayed_through_the_service(limited(NOW + timedelta(hours=2)), **options)

    assert spoken == []
    notices = [payload for name, payload in events if name == "proactive.notification"]
    assert len(notices) == 1 and "Kontingent" in notices[0]["text"]
    assert notices[0]["spoken"] is False


@pytest.mark.parametrize("mode", list(Mode))
@pytest.mark.parametrize("privacy_mode", ["balanced", "private", "offline"])
async def test_a_failed_turn_goes_through_the_real_service_as_a_toast_that_is_never_spoken(
    mode: Mode, privacy_mode: str
) -> None:
    spoken, events = await relayed_through_the_service(
        failed("unavailable"), mode=mode, privacy_mode=privacy_mode
    )

    assert spoken == []  # the reply was spoken by the orchestrator; this is only the record
    (notice,) = [payload for name, payload in events if name == "proactive.notification"]
    assert notice["priority"] == "resources"  # not `security`, which no quiet rule holds back
    assert notice["kind"] == "urgent" and notice["spoken"] is False

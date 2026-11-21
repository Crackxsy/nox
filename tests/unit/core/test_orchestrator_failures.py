"""A turn that no model answers: the user is told why and what to do, not left with silence."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from nox.ai.base import AiChunk, AiRequest
from nox.ai.errors import (
    BudgetExceededError,
    NoProviderAvailableError,
    NoProviderCause,
    ProviderError,
)
from nox.ai.limits import ProviderLimit
from nox.core.events import E, Event
from nox.core.orchestrator import NOTICE_PROVIDER
from tests.unit.fakes import FakeBus, FakeRouter
from tests.unit.fakes import make_orchestrator as make


class FailingRouter(FakeRouter):
    """Streams `before` (if any) and then fails with `error`."""

    def __init__(self, bus: FakeBus, error: Exception, before: tuple[str, ...] = ()) -> None:
        super().__init__(bus)
        self.error = error
        self.before = before

    async def stream(self, request: AiRequest) -> AsyncIterator[AiChunk]:
        self.requests.append(request)
        for delta in self.before:
            yield AiChunk(request_id=request.request_id, delta=delta)
        raise self.error


def failing(error: Exception, before: tuple[str, ...] = (), **kw: object):  # type: ignore[no-untyped-def]
    orch, bus, router, speaker, turns = make(**kw)
    orch.router = FailingRouter(bus, error, before)
    return orch, bus, speaker, turns


def failed_events(bus: FakeBus) -> list[dict[str, object]]:
    return [e.payload for e in bus.published if e.name == E.AI_TURN_FAILED]


async def test_a_typed_turn_gets_the_reason_and_the_next_step_as_its_reply() -> None:
    orch, bus, _, turns = failing(NoProviderAvailableError("boom", cause=NoProviderCause.FAILED))
    await orch.start()
    chunks: list[str] = []

    turn = await orch.handle_text("Erklär mir kurz Python", speak=False, on_chunk=chunks.append)

    assert "Das Modell antwortet gerade nicht" in turn.response
    assert "".join(chunks) == turn.response  # the chat shows exactly what the turn recorded
    assert turn.provider == NOTICE_PROVIDER and turn.degraded
    # The question is kept; the notice is Nox's, not an answer, and stays out of the history.
    assert [row[1] for row in turns.rows] == ["user"]
    assert failed_events(bus) == []  # nobody asked for speech: nothing to say aloud
    await orch.stop()


async def test_a_spoken_turn_is_answered_aloud_with_the_reason() -> None:
    orch, bus, speaker, _ = failing(
        NoProviderAvailableError("x", cause=NoProviderCause.UNAVAILABLE)
    )
    await orch.start()

    turn = await orch.handle_text("Erklär mir kurz Python", language="en")

    assert "No brain available" in turn.response
    # A reply to what the user just said: spoken, not a proactive hint.
    assert [s.text for s in speaker.said] == [turn.response]
    assert failed_events(bus) == [
        {"cause": "unavailable", "language": "en", "until": "", "estimated": False}
    ]
    await orch.stop()


async def test_a_limit_names_its_end_and_is_passed_on_for_the_notification() -> None:
    until = datetime.now(UTC) + timedelta(hours=3)
    limit = ProviderLimit(provider="claude_code", kind="five_hour", until=until)
    error = NoProviderAvailableError("limit", cause=NoProviderCause.LIMIT, limit=limit)
    orch, bus, _, _ = failing(error)
    await orch.start()

    turn = await orch.handle_text("Erklär mir kurz Python")

    assert "Kontingent" in turn.response and "Uhr" in turn.response
    (payload,) = failed_events(bus)
    assert payload["cause"] == "limit" and payload["until"] == until.isoformat()
    await orch.stop()


async def test_a_spent_background_budget_is_told_as_such() -> None:
    orch, _, _, _ = failing(BudgetExceededError("share used"))
    await orch.start()

    turn = await orch.handle_text("Erklär mir kurz Python", speak=False)

    assert "Tagesbudget" in turn.response
    await orch.stop()


async def test_an_answer_cut_short_keeps_its_words_and_gets_the_reason_after_them() -> None:
    error = NoProviderAvailableError("limit", cause=NoProviderCause.FAILED)
    orch, _, speaker, turns = failing(error, before=("Das Wetter ist",))
    await orch.start()
    chunks: list[str] = []

    turn = await orch.handle_text("Wie wird das Wetter?", speak=False, on_chunk=chunks.append)

    assert turn.response.startswith("Das Wetter ist Das Modell antwortet gerade nicht")
    assert chunks[0] == "Das Wetter ist" and chunks[1].startswith(" Das Modell")
    assert [row[1] for row in turns.rows] == ["user"]
    assert [s.text for s in speaker.said] == []  # typed turn: no speech asked for
    await orch.stop()


async def test_a_failed_turn_is_not_handed_to_memory_capture() -> None:
    orch, _, _, _ = failing(NoProviderAvailableError("x", cause=NoProviderCause.FAILED))
    seen: list[tuple[str, str]] = []
    orch.on_turn_complete = lambda user, reply: seen.append((user, reply))
    await orch.start()

    await orch.handle_text("Ich heisse Beispiel", speak=False)

    assert seen == []
    await orch.stop()


async def test_an_unexpected_error_is_told_honestly_and_not_as_the_models_fault() -> None:
    orch, bus, speaker, _ = failing(RuntimeError("bug"))
    await orch.start()

    await orch._voice_turn("Erklär mir kurz Python", "de")

    assert failed_events(bus) == [
        {"cause": "internal", "language": "de", "until": "", "estimated": False}
    ]
    (said,) = [s.text for s in speaker.said]
    assert said.startswith("Etwas ist schiefgegangen") and "Modell antwortet" not in said
    await orch.stop()


async def test_a_providers_own_error_that_escapes_is_told_as_the_models() -> None:
    orch, bus, speaker, _ = failing(ProviderError("claude_code", "exit 3"))
    await orch.start()

    await orch._voice_turn("Erklär mir kurz Python", "de")

    assert failed_events(bus)[0]["cause"] == "failed"
    assert "Das Modell antwortet gerade nicht" in speaker.said[0].text
    await orch.stop()


async def test_a_speaker_that_fails_is_not_mistaken_for_a_model_and_does_not_end_the_turn() -> None:
    orch, bus, speaker, _ = failing(RuntimeError("bug"))

    async def broken(_request: object) -> None:
        raise OSError("no audio device")

    speaker.say = broken  # type: ignore[method-assign]
    await orch.start()

    await orch._voice_turn("Erklär mir kurz Python", "de")  # must not raise

    assert failed_events(bus)[0]["cause"] == "internal"
    await orch.stop()


async def test_a_notice_is_not_spoken_when_the_gate_says_no_but_still_shown() -> None:
    error = NoProviderAvailableError("x", cause=NoProviderCause.UNAVAILABLE)
    orch, bus, speaker, _ = failing(error)
    orch.failure_may_speak = lambda: False
    await orch.start()

    turn = await orch.handle_text("Erklär mir kurz Python")

    assert speaker.said == []
    assert "Kein Gehirn verfügbar" in turn.response and failed_events(bus)
    await orch.stop()


async def test_the_failure_is_said_every_time_the_user_asks_not_once_per_five_minutes() -> None:
    orch, _, speaker, _ = failing(NoProviderAvailableError("x", cause=NoProviderCause.FAILED))
    await orch.start()

    await orch.handle_text("Erklär mir kurz Python")
    await orch.handle_text("Erklär mir noch etwas")

    assert len(speaker.said) == 2
    await orch.stop()


async def test_after_the_kill_switch_a_stopped_turn_stays_silent() -> None:
    orch, bus, _, _ = failing(RuntimeError("bug"))
    await orch.start()
    await bus.publish(Event(name=E.SECURITY_KILL_SWITCH, payload={"by": "hotkey"}))

    await orch._voice_turn(
        "Erklär mir kurz Python", "de"
    )  # raises "safe mode": the user pressed the kill switch

    assert failed_events(bus) == []
    await orch.stop()


@pytest.mark.parametrize(("language", "word"), [("de", "Kein Gehirn"), ("en", "No brain")])
async def test_the_reply_follows_the_language_of_the_turn(language: str, word: str) -> None:
    orch, _, _, _ = failing(NoProviderAvailableError("x", cause=NoProviderCause.UNAVAILABLE))
    await orch.start()

    turn = await orch.handle_text("?", language=language, speak=False)

    assert word in turn.response
    await orch.stop()

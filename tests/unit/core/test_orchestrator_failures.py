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
    assert [row[1] for row in turns.rows] == ["user", "assistant"]
    assert failed_events(bus) == []  # nobody asked for speech: nothing to say aloud
    await orch.stop()


async def test_a_spoken_turn_leaves_the_voice_to_the_proactive_rules() -> None:
    orch, bus, speaker, _ = failing(
        NoProviderAvailableError("x", cause=NoProviderCause.UNAVAILABLE)
    )
    await orch.start()

    turn = await orch.handle_text("Erklär mir kurz Python", language="en")

    assert "No brain available" in turn.response
    assert speaker.said == []  # not here: mode, privacy and quiet hours decide in one place
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
    orch, _, _, turns = failing(error, before=("Das Wetter ist",))
    await orch.start()
    chunks: list[str] = []

    turn = await orch.handle_text("Wie wird das Wetter?", speak=False, on_chunk=chunks.append)

    assert turn.response.startswith("Das Wetter ist Das Modell antwortet gerade nicht")
    assert chunks[0] == "Das Wetter ist" and chunks[1].startswith(" Das Modell")
    assert turns.rows[-1][2] == turn.response
    await orch.stop()


async def test_a_failed_turn_is_not_handed_to_memory_capture() -> None:
    orch, _, _, _ = failing(NoProviderAvailableError("x", cause=NoProviderCause.FAILED))
    seen: list[tuple[str, str]] = []
    orch.on_turn_complete = lambda user, reply: seen.append((user, reply))
    await orch.start()

    await orch.handle_text("Ich heisse Beispiel", speak=False)

    assert seen == []
    await orch.stop()


async def test_an_unexpected_error_in_a_spoken_turn_is_announced_not_only_logged() -> None:
    orch, bus, _, _ = failing(RuntimeError("bug"))
    await orch.start()

    await orch._voice_turn("Erklär mir kurz Python", "de")

    assert failed_events(bus) == [
        {"cause": "failed", "language": "de", "until": "", "estimated": False}
    ]
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

"""RulesProvider: deterministic intents, DE/EN, degraded flags, classify role, streaming."""

from __future__ import annotations

from datetime import datetime

import pytest

from nox.ai.base import AiRole
from nox.ai.providers.rules import RulesProvider, classify_intent, detect_language
from nox.core.events import HealthStatus

from .conftest import make_request

FIXED = datetime(2026, 9, 9, 19, 31)


@pytest.fixture
def rules() -> RulesProvider:
    return RulesProvider(clock=lambda: FIXED)


async def test_info_and_health_always_available(rules: RulesProvider) -> None:
    assert rules.info.id == "rules"
    assert rules.info.local is True
    assert set(rules.info.roles) == {AiRole.CLASSIFY, AiRole.CHAT, AiRole.BACKGROUND}
    info = await rules.health()
    assert info.status is HealthStatus.AVAILABLE


async def test_every_response_is_marked_degraded(rules: RulesProvider) -> None:
    response = await rules.complete(make_request("Hallo"))
    assert response.degraded is True
    assert response.degraded_reason == "rules"
    assert response.provider == "rules"
    assert response.tokens_in is None and response.tokens_out is None


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("Hallo Nox", "greeting"),
        ("hey", "greeting"),
        ("Guten Morgen", "greeting"),
        ("Wie spät ist es?", "time"),
        ("what time is it", "time"),
        ("Wie geht's dir?", "status"),
        ("how are you", "status"),
        ("Wer bist du?", "identity"),
        ("who are you", "identity"),
        ("Schreib mir ein Gedicht über Rocket League", "unknown"),
    ],
)
def test_classify_intent(text: str, intent: str) -> None:
    assert classify_intent(text) == intent


def test_detect_language_hint_wins_then_heuristic() -> None:
    assert detect_language("what time is it", "de") == "de"
    assert detect_language("Wie spät ist es", None) == "de"
    assert detect_language("what time is it", None) == "en"


async def test_time_answer_de_uses_injected_clock(rules: RulesProvider) -> None:
    response = await rules.complete(make_request("Wie spät ist es?", metadata={"language": "de"}))
    assert response.text == "Es ist 19:31 Uhr, Mittwoch, 9. September 2026."


async def test_time_answer_en(rules: RulesProvider) -> None:
    response = await rules.complete(make_request("what time is it", metadata={"language": "en"}))
    assert response.text.startswith("It's 19:31, Wednesday, September 09, 2026")


async def test_fallback_is_honest_about_being_offline(rules: RulesProvider) -> None:
    de = await rules.complete(make_request("Erklär mir Quantenphysik", metadata={"language": "de"}))
    en = await rules.complete(make_request("Explain quantum physics", metadata={"language": "en"}))
    assert de.text.startswith("Ich bin gerade offline")
    assert en.text.startswith("I'm offline right now")


async def test_language_falls_back_to_heuristic_without_hint(rules: RulesProvider) -> None:
    response = await rules.complete(make_request("who are you"))
    assert response.text.startswith("I'm Nox")
    response = await rules.complete(make_request("wer bist du"))
    assert response.text.startswith("Ich bin Nox")


async def test_status_includes_injected_status_source() -> None:
    rules = RulesProvider(clock=lambda: FIXED, status_source=lambda: {"ollama": "down"})
    response = await rules.complete(make_request("Status?", metadata={"language": "en"}))
    assert "ollama: down" in response.text


async def test_classify_role_returns_intent_label(rules: RulesProvider) -> None:
    response = await rules.complete(make_request("Wie spät ist es?", role=AiRole.CLASSIFY))
    assert response.text == "time"
    response = await rules.complete(make_request("Bau mir eine Rakete", role=AiRole.CLASSIFY))
    assert response.text == "unknown"


async def test_stream_yields_text_then_done(rules: RulesProvider) -> None:
    chunks = [c async for c in rules.stream(make_request("Hallo", metadata={"language": "de"}))]
    assert [c.done for c in chunks] == [False, True]
    assert chunks[0].delta.startswith("Hey.")
    assert chunks[1].delta == ""

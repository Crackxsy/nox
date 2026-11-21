"""A plan limit through the real provider and the real router, against the CLI's own wording.

The real CLI refuses a request that a plan limit stopped with a message of its own - model
`<synthetic>`, an `error` field, a sentence like "You've hit your limit" - not with a model's
words. That sentence must reach neither the chat nor the speakers: the turn falls back to the next
provider or ends in a limit error, and the user is told by Nox's own notice.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nox.ai.base import AiChunk, AiRequest, AiRole, Message
from nox.ai.config import ClaudeCodeConfig, RouterConfig
from nox.ai.errors import NoProviderAvailableError, NoProviderCause
from nox.ai.providers import claude_persistent
from nox.ai.providers.claude_code import ClaudeCodeProvider
from nox.ai.router import DefaultRouter
from nox.core.events import E
from tests.unit.fakes import FakeBus

from .conftest import FakeProvider

FAKE = Path(__file__).with_name("fake_claude.py")
NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
RAW_SENTENCE = "You've hit your limit"


def request(text: str, request_id: str = "r1") -> AiRequest:
    return AiRequest(
        request_id=request_id,
        role=AiRole.CHAT,
        messages=[
            Message(role="system", content="You are Nox."),
            Message(role="user", content=text),
        ],
        metadata={"session_id": "conversation-1", "language": "de"},
    )


async def collect(stream: AsyncIterator[AiChunk]) -> str:
    return "".join([chunk.delta async for chunk in stream])


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ClaudeCodeProvider:
    monkeypatch.setattr(
        claude_persistent,
        "INHERITED_ENV",
        claude_persistent.INHERITED_ENV | {"FAKE_RESET", "FAKE_NO_EVENT"},
    )
    monkeypatch.setenv("FAKE_RESET", str(datetime(2026, 3, 10, 15, 0, tzinfo=UTC).timestamp()))
    return ClaudeCodeProvider(
        ClaudeCodeConfig(model="sonnet", prewarm=False),
        env=dict(os.environ),
        command_override=[sys.executable, str(FAKE)],
        workdir=tmp_path / "claude",
        now=lambda: NOW,
    )


def router_of(claude: ClaudeCodeProvider, bus: FakeBus, *others: FakeProvider) -> DefaultRouter:
    return DefaultRouter(
        [claude, *others],
        bus,  # type: ignore[arg-type]
        RouterConfig(fallback_chain=["claude_code", *(p.pid for p in others)]),
        wall_clock=lambda: NOW,
    )


async def test_the_clis_limit_sentence_never_reaches_the_user_and_the_turn_falls_back(
    claude: ClaudeCodeProvider,
) -> None:
    bus = FakeBus()
    router = router_of(claude, bus, FakeProvider("ollama", text="Lokale Antwort"))

    said = await collect(router.stream(request("LIMITNOW")))
    again = await collect(router.stream(request("Hallo", "r2")))

    assert said == again == "Lokale Antwort"
    assert RAW_SENTENCE not in said
    (announced,) = bus.of(E.AI_PROVIDER_LIMITED)
    assert announced.payload["until"] == datetime(2026, 3, 10, 15, 0, tzinfo=UTC).isoformat()
    assert announced.payload["fallback"] == "local"
    await claude.conversation.close()


async def test_without_a_fallback_the_turn_ends_in_a_limit_error_and_not_in_the_sentence(
    claude: ClaudeCodeProvider,
) -> None:
    router = router_of(claude, FakeBus())
    received: list[str] = []

    with pytest.raises(NoProviderAvailableError) as caught:
        async for chunk in router.stream(request("LIMITNOW")):
            received.append(chunk.delta)

    assert received == []
    assert caught.value.cause is NoProviderCause.LIMIT
    assert caught.value.limit is not None and caught.value.limit.kind == "five_hour"
    await claude.conversation.close()


async def test_a_limit_told_only_by_the_clis_sentence_still_ends_the_turn_and_names_the_reset(
    claude: ClaudeCodeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_NO_EVENT", "1")
    claude._env = dict(os.environ)  # noqa: SLF001 - the session was configured before the patch
    claude.conversation._base_env = dict(os.environ)  # noqa: SLF001
    router = router_of(claude, FakeBus(), FakeProvider("ollama", text="Lokale Antwort"))

    said = await collect(router.stream(request("LIMITNOW")))

    assert said == "Lokale Antwort"
    (limit,) = router.limits().values()
    # "resets 11pm (Europe/Berlin)" asked at 12:00 UTC: tonight at 23:00 in Berlin.
    assert limit.until == datetime(2026, 3, 10, 22, 0, tzinfo=UTC) and not limit.estimated
    await claude.conversation.close()


async def test_a_limit_after_the_first_words_ends_the_answer_in_nox_own_words(
    claude: ClaudeCodeProvider,
) -> None:
    router = router_of(claude, FakeBus(), FakeProvider("ollama", text="Lokale Antwort"))

    said = await collect(router.stream(request("LIMITLATE")))

    assert said.startswith("Hallo Welt.") and "Kontingent ist aufgebraucht" in said
    assert RAW_SENTENCE not in said
    await claude.conversation.close()

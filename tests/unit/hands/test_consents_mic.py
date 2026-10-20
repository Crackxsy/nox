"""The consent store and the `mic.record_long` gate (§ 201 StGB).

The negative test: with no stored consent for a context, the tool must refuse and never record.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from nox.data.db import Database
from nox.hands.consents import ConsentRepository
from nox.hands.mic import register_mic_tool
from nox.tools.registry import ToolRegistry


@pytest.fixture
def consents(tmp_path) -> Iterator[ConsentRepository]:  # noqa: ANN001
    db = Database(tmp_path / "nox.db")
    db.migrate()
    yield ConsentRepository(db)
    db.close()


def test_grant_and_has_consent(consents: ConsentRepository) -> None:
    assert not consents.has_consent("lesson:math")
    consents.grant(context="lesson:math", who="class 10b", wording="recording ok for notes")
    assert consents.has_consent("lesson:math")
    # Consent is per context: another context is still ungranted.
    assert not consents.has_consent("lesson:physics")


def test_revoke(consents: ConsentRepository) -> None:
    consents.grant(context="lesson:math", who="x", wording="ok")
    assert consents.revoke("lesson:math") == 1
    assert not consents.has_consent("lesson:math")


def test_grant_needs_all_fields(consents: ConsentRepository) -> None:
    with pytest.raises(ValueError):
        consents.grant(context="", who="x", wording="ok")


def test_mic_refuses_without_consent(consents: ConsentRepository) -> None:
    registry = ToolRegistry()
    recorded: list[int] = []

    async def transcribe(seconds: int) -> str:
        recorded.append(seconds)
        return "transcript"

    register_mic_tool(registry, consents=consents, transcribe=transcribe)
    handler = registry.get("mic.record_long").handler  # type: ignore[union-attr]

    result = asyncio.run(handler({"context": "lesson:math", "seconds": 30}))
    assert result["ok"] is False and result["error"] == "consent.missing"
    assert recorded == []  # nothing was recorded


def test_mic_records_with_consent_and_drops_raw_audio(consents: ConsentRepository) -> None:
    registry = ToolRegistry()
    indicators: list[bool] = []

    async def transcribe(seconds: int) -> str:
        return "the lesson transcript"

    async def indicator(on: bool) -> None:
        indicators.append(on)

    consents.grant(context="lesson:math", who="class", wording="ok")
    register_mic_tool(registry, consents=consents, transcribe=transcribe, indicator=indicator)
    handler = registry.get("mic.record_long").handler  # type: ignore[union-attr]

    result = asyncio.run(handler({"context": "lesson:math", "seconds": 30}))
    assert result["ok"] is True
    assert result["transcript"] == "the lesson transcript"
    assert result["raw_audio_kept"] is False
    assert indicators == [True, False]  # indicator on while recording, off after


def test_mic_is_not_rememberable() -> None:
    registry = ToolRegistry()

    async def transcribe(seconds: int) -> str:
        return ""

    class _NoConsent:
        def has_consent(self, context: str) -> bool:
            return False

    register_mic_tool(registry, consents=_NoConsent(), transcribe=transcribe)  # type: ignore[arg-type]
    assert registry.get("mic.record_long").rememberable is False  # type: ignore[union-attr]

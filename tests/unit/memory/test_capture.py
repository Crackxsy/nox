"""`nox.memory.capture`: Nox learns facts about the user from conversation, and keeps them in the
prompt. The model that extracts is faked; what is pinned here is what gets stored, what does not,
and what the prompt says."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from nox.ai.base import AiRequest, AiResponse
from nox.data.db import Database
from nox.data.repos import MemoryItemRepository
from nox.memory.capture import FactCapture, UserProfile, extract_facts_json, normalise_fact
from nox.memory.importance import MemoryType
from nox.memory.items import MemoryService


class _Gate:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed

    def allows_memory_write(self) -> bool:
        return self.allowed


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "nox.db")
    database.migrate()
    yield database
    database.close()


def _capture(db: Database, answer: str, *, allowed: bool = True) -> tuple[FactCapture, list]:
    repo = MemoryItemRepository(db)
    seen: list[AiRequest] = []

    async def complete(request: AiRequest) -> AiResponse:
        seen.append(request)
        return AiResponse(request_id=request.request_id, provider="fake", text=answer, latency_ms=1)

    capture = FactCapture(
        memory=MemoryService(repo, _Gate(allowed)),
        profile=UserProfile(repo, user_name="Lena"),
        complete=complete,
        user_name="Lena",
    )
    return capture, seen


def test_the_json_the_model_sends_back_is_read_tolerantly() -> None:
    text = (
        'Sure:\n[{"text": "Lena spielt Rocket League.", "type": "preference", '
        '"confidence": 0.9}, 7, {"text": "", "type": "user"}]'
    )
    facts = extract_facts_json(text)
    assert [(f.text, f.type, f.confidence) for f in facts] == [
        ("Lena spielt Rocket League.", MemoryType.PREFERENCE, 0.9)
    ]
    assert extract_facts_json("I could not find anything.") == []
    assert extract_facts_json('[{"text": "x", "type": "stream"}]')[0].type is MemoryType.USER


async def test_facts_from_a_conversation_are_stored_and_reach_the_prompt(db: Database) -> None:
    capture, seen = _capture(
        db,
        '[{"text": "Lena\' bester Freund heißt Tim.", "type": "relationship",'
        ' "confidence": 0.95},'
        ' {"text": "Lena streamt Minecraft.", "type": "preference", "confidence": 0.8}]',
    )
    capture.observe("Mein bester Freund Tim und ich streamen heute Minecraft.", "Klingt gut.")
    stored = await capture.flush()

    assert stored == 2 and capture.captured == 2
    assert "Mein bester Freund Tim" in seen[0].messages[-1].content
    block = capture.profile.block()
    assert "What you know about Lena" in block
    assert "Lena' bester Freund heißt Tim." in block and "(relationship," in block


async def test_a_fact_already_known_is_not_stored_twice(db: Database) -> None:
    capture, _ = _capture(db, '[{"text": "Lena streamt Minecraft!", "type": "preference"}]')
    await capture.memory.create("Lena streamt Minecraft.", type=MemoryType.PREFERENCE)
    capture.observe("Ich streame Minecraft.", "Ja.")
    assert await capture.flush() == 0
    assert capture.skipped_duplicates == 1
    assert normalise_fact("Lena streamt Minecraft!") == normalise_fact(
        "Lena streamt  Minecraft."
    )


async def test_known_facts_are_handed_to_the_extractor_so_it_can_skip_them(db: Database) -> None:
    capture, seen = _capture(db, "[]")
    await capture.memory.create("Lena wohnt in Witten.", type=MemoryType.USER)
    capture.observe("Hallo", "Hallo Lena.")
    await capture.flush()
    assert "Already known:" in seen[0].messages[0].content
    assert "Lena wohnt in Witten." in seen[0].messages[0].content


async def test_low_confidence_guesses_are_dropped(db: Database) -> None:
    capture, _ = _capture(db, '[{"text": "Lena mag vielleicht Katzen.", "confidence": 0.2}]')
    capture.observe("Katzen sind okay.", "Verstehe.")
    assert await capture.flush() == 0


async def test_nothing_is_stored_while_privacy_forbids_memory_writes(db: Database) -> None:
    capture, _ = _capture(
        db, '[{"text": "Lena hat Geburtstag im Mai.", "type": "event"}]', allowed=False
    )
    capture.observe("Ich hab im Mai Geburtstag.", "Notiert.")
    assert await capture.flush() == 0
    assert db.fetch_all("SELECT * FROM memory_items") == []


async def test_capture_runs_in_the_background_after_the_conversation_settles(db: Database) -> None:
    capture, seen = _capture(db, '[{"text": "Lena lernt Delphi.", "type": "user"}]')
    capture.settle_s = 0.05
    capture.observe("Ich lerne gerade Delphi.", "Gute Wahl.")
    assert seen == []  # observe() returns before any model is asked
    await asyncio.sleep(0.3)
    assert capture.captured == 1 and len(seen) == 1
    await capture.stop()


async def test_an_extractor_failure_is_logged_not_raised(db: Database) -> None:
    repo = MemoryItemRepository(db)

    async def broken(_request: AiRequest) -> AiResponse:
        raise RuntimeError("model down")

    capture = FactCapture(
        memory=MemoryService(repo, _Gate()),
        profile=UserProfile(repo, user_name="Lena"),
        complete=broken,
        user_name="Lena",
        settle_s=0.05,
    )
    capture.observe("Hallo", "Hallo.")
    await asyncio.sleep(0.3)
    assert "RuntimeError" in capture.last_error
    await capture.stop()


def test_the_profile_block_stays_within_its_budget(db: Database) -> None:
    repo = MemoryItemRepository(db)
    for i in range(40):
        repo.add(type="user", text=f"Fakt Nummer {i} über Lena, ausführlich.", importance=0.5)
    block = UserProfile(repo, user_name="Lena", max_chars=400).block()
    assert len(block) <= 400 and block.count("\n- ") >= 3
    roomy = UserProfile(repo, user_name="Lena", max_chars=8000).block()
    assert roomy.count("\n- ") == 40

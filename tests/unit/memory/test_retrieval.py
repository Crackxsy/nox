"""nox.memory.retrieval.RetrievalService: ranked hydration, hard token cap, honest not-found
(ST-07-05 AC2/AC6)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from nox.data.db import Database
from nox.memory.embeddings import SearchHit
from nox.memory.retrieval import RetrievalService, format_context


class FakeEmbeddings:
    def __init__(self, hits: list[SearchHit]) -> None:
        self.hits = hits

    async def search(self, query: str, *, k: int = 10, kind: str | None = None) -> list[SearchHit]:
        return self.hits[:k]


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "nox.db")
    database.migrate()
    yield database
    database.close()


async def test_retrieve_hydrates_and_ranks(db: Database) -> None:
    db.execute(
        "INSERT INTO memory_items (id, type, text, importance, source, created_at, privacy_class) "
        "VALUES (1, 'user', 'the kill switch is 100ms', 0.7, 'sess-1', '2026-01-01', 'normal')"
    )
    hits = [SearchHit(kind="memory_item", ref_id=1, score=0.9, via="vector")]
    svc = RetrievalService(db, FakeEmbeddings(hits))
    result = await svc.retrieve("kill switch")
    assert result.found is True
    assert result.limited is False
    assert result.items[0].text == "the kill switch is 100ms"
    assert result.items[0].source == "sess-1"


async def test_retrieve_honest_not_found_when_nothing_relevant(db: Database) -> None:
    svc = RetrievalService(db, FakeEmbeddings([]))
    result = await svc.retrieve("anything at all")
    assert result.found is False
    assert result.items == []


async def test_retrieve_marks_limited_when_fts_used(db: Database) -> None:
    db.execute(
        "INSERT INTO vault_index (path, mtime, hash, title, type, tags_json, indexed_at) "
        "VALUES ('n.md', 0, 'h', 'N', '', '[]', '2026-01-01')"
    )
    db.execute(
        "INSERT INTO vault_chunks (id, path, ord, text, hash) VALUES (1, 'n.md', 0, 'text', 'h')"
    )
    hits = [SearchHit(kind="vault_chunk", ref_id=1, score=0.5, via="fts")]
    svc = RetrievalService(db, FakeEmbeddings(hits))
    result = await svc.retrieve("query")
    assert result.limited is True


async def test_retrieve_respects_token_budget(db: Database) -> None:
    for i in range(1, 5):
        db.execute(
            "INSERT INTO memory_items "
            "(id, type, text, importance, source, created_at, privacy_class) "
            "VALUES (?, 'user', ?, 0.5, 's', '2026-01-01', 'normal')",
            (i, "x" * 500),
        )
    hits = [
        SearchHit(kind="memory_item", ref_id=i, score=1.0 - i * 0.1, via="vector")
        for i in range(1, 5)
    ]
    svc = RetrievalService(
        db, FakeEmbeddings(hits), max_tokens=100, chars_per_token=4
    )  # ~400 chars
    result = await svc.retrieve("q", k=4)
    assert len(result.items) == 1  # first item alone already fills the budget


async def test_retrieve_empty_query_returns_not_found(db: Database) -> None:
    svc = RetrievalService(db, FakeEmbeddings([]))
    result = await svc.retrieve("   ")
    assert result.found is False


def test_format_context_marks_limited() -> None:
    from nox.memory.retrieval import RetrievalResult, RetrievedItem

    result = RetrievalResult(
        query="q",
        items=[
            RetrievedItem(
                kind="vault_chunk", ref_id=1, score=0.5, text="t", source="n.md", via="fts"
            )
        ],
        found=True,
        limited=True,
    )
    text = format_context(result)
    assert text.startswith("(degraded: keyword search only)")


def test_format_context_empty_when_no_items() -> None:
    from nox.memory.retrieval import RetrievalResult

    assert format_context(RetrievalResult(query="q")) == ""


# -- relevance floor, margin and the prompt budget ------------------------------------------------


def _note(db: Database, ref_id: int, text: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO vault_index (path, mtime, hash, title, type, tags_json, indexed_at) "
        "VALUES ('n.md', 0, 'h', 'N', '', '[]', '2026-01-01')"
    )
    db.execute(
        "INSERT INTO vault_chunks (id, path, ord, text, hash) VALUES (?, 'n.md', ?, ?, 'h')",
        (ref_id, ref_id, text),
    )


async def test_small_talk_retrieves_nothing_instead_of_padding_the_prompt(db: Database) -> None:
    # Every note is "nearest" to a greeting; none of them is relevant to it, and every retrieved
    # token costs time to the first spoken word.
    for ref_id in (1, 2, 3):
        _note(db, ref_id, f"Notiz {ref_id}")
    hits = [
        SearchHit(kind="vault_chunk", ref_id=1, score=0.44, via="vector"),
        SearchHit(kind="vault_chunk", ref_id=2, score=0.43, via="vector"),
        SearchHit(kind="vault_chunk", ref_id=3, score=0.41, via="vector"),
    ]
    result = await RetrievalService(db, FakeEmbeddings(hits)).retrieve("Hallo")
    assert result.found is False
    assert result.dropped == 3
    assert result.relevance == 0.0
    assert format_context(result) == ""


async def test_only_the_hits_close_to_the_best_one_are_kept(db: Database) -> None:
    _note(db, 1, "Das Projekt nutzt SQLite.")
    _note(db, 2, "Nudelteig ruht 30 Minuten.")
    hits = [
        SearchHit(kind="vault_chunk", ref_id=1, score=0.81, via="vector"),
        SearchHit(kind="vault_chunk", ref_id=2, score=0.67, via="vector"),
    ]
    result = await RetrievalService(db, FakeEmbeddings(hits)).retrieve("Welche Datenbank?")
    assert [item.ref_id for item in result.items] == [1]
    assert result.dropped == 1
    assert result.relevance == pytest.approx(0.81)


async def test_keyword_hits_are_never_measured_against_the_vector_floor(db: Database) -> None:
    # BM25 ranks are an unbounded, unrelated scale; filtering them by a cosine threshold would
    # silently empty every degraded result.
    _note(db, 1, "Das Projekt nutzt SQLite.")
    hits = [SearchHit(kind="vault_chunk", ref_id=1, score=0.02, via="fts")]
    result = await RetrievalService(db, FakeEmbeddings(hits)).retrieve("Datenbank")
    assert result.found is True
    assert result.limited is True
    assert result.relevance == 0.0  # "unknown", not "irrelevant"


async def test_the_budget_can_be_retuned_at_runtime(db: Database) -> None:
    _note(db, 1, "A" * 400)
    _note(db, 2, "B" * 400)
    hits = [
        SearchHit(kind="vault_chunk", ref_id=1, score=0.81, via="vector"),
        SearchHit(kind="vault_chunk", ref_id=2, score=0.80, via="vector"),
    ]
    service = RetrievalService(db, FakeEmbeddings(hits))
    service.set_budget(max_tokens=256, min_score=0.0, score_margin=1.0)
    assert service.max_tokens == 256
    result = await service.retrieve("egal")
    assert [item.ref_id for item in result.items] == [1, 2]
    service.set_budget(max_tokens=100, min_score=0.0, score_margin=1.0)
    result = await service.retrieve("egal")
    assert [item.ref_id for item in result.items] == [1]  # second chunk no longer fits

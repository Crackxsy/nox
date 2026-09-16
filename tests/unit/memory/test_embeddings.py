"""nox.memory.embeddings: FTS5 fallback branching when Ollama is unreachable (ST-07-02 AC2), and
the vector path against a fake provider (no live Ollama dependency for this suite - SP-07 exercises
the real provider separately)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from nox.data.db import Database
from nox.memory.embeddings import EmbeddingService


class FakeProvider:
    def __init__(self, *, fail: bool = False, dim: int = 8) -> None:
        self.fail = fail
        self.dim = dim
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        self.calls.append(texts)
        if self.fail:
            raise ConnectionError("ollama unreachable")
        return [[float(len(t) % 7) + i * 0.01 for i in range(self.dim)] for t in texts]


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "nox.db")
    database.migrate()
    yield database
    database.close()


async def test_available_false_without_provider(db: Database) -> None:
    svc = EmbeddingService(db, None, dimensions=8)
    assert await svc.available() is False


async def test_available_false_when_provider_unreachable(db: Database) -> None:
    svc = EmbeddingService(db, FakeProvider(fail=True), dimensions=8)
    assert await svc.available() is False


async def test_available_true_with_working_provider(db: Database) -> None:
    svc = EmbeddingService(db, FakeProvider(), dimensions=8)
    assert await svc.available() is True


async def test_embed_and_store_noop_without_provider(db: Database) -> None:
    svc = EmbeddingService(db, None, dimensions=8)
    ok = await svc.embed_and_store("memory_item", 1, "hello")
    assert ok is False


async def test_search_falls_back_to_fts_when_provider_fails(db: Database) -> None:
    db.execute(
        "INSERT INTO memory_items (type, text, importance, source, created_at, privacy_class) "
        "VALUES ('user', 'kill switch latency target is 100ms', 0.5, 'test', '2026-01-01', "
        "'normal')"
    )
    svc = EmbeddingService(db, FakeProvider(fail=True), dimensions=8)
    hits = await svc.search("kill switch latency", k=5)
    assert hits
    assert hits[0].via == "fts"
    assert hits[0].kind == "memory_item"


async def test_search_falls_back_to_fts_without_provider(db: Database) -> None:
    db.execute(
        "INSERT INTO vault_index (path, mtime, hash, title, type, tags_json, indexed_at) "
        "VALUES ('n.md', 0, 'h', 'N', '', '[]', '2026-01-01')"
    )
    db.execute(
        "INSERT INTO vault_chunks (path, ord, text, hash) "
        "VALUES ('n.md', 0, 'piper tts engine decision', 'h')"
    )
    svc = EmbeddingService(db, None, dimensions=8)
    hits = await svc.search("piper tts engine", k=5)
    assert hits
    assert hits[0].via == "fts"
    assert hits[0].kind == "vault_chunk"


async def test_search_empty_query_returns_nothing(db: Database) -> None:
    svc = EmbeddingService(db, None, dimensions=8)
    assert await svc.search("   ") == []


async def test_embed_and_store_then_remove(db: Database) -> None:
    provider = FakeProvider()
    svc = EmbeddingService(db, provider, dimensions=8)
    ok = await svc.embed_and_store("memory_item", 1, "some text")
    assert ok is True
    row = db.fetch_one("SELECT id FROM vec_map WHERE kind='memory_item' AND ref_id=1")
    assert row is not None
    svc.remove("memory_item", 1)
    row_after = db.fetch_one("SELECT id FROM vec_map WHERE kind='memory_item' AND ref_id=1")
    assert row_after is None


async def test_embed_and_store_many_batches_one_request_per_32(db: Database) -> None:
    """The first vault scan embedded one chunk per HTTP request; `/api/embed` takes a list."""
    provider = FakeProvider()
    svc = EmbeddingService(db, provider, dimensions=8)
    items = [(i, f"chunk {i}") for i in range(1, 71)]
    stored = await svc.embed_and_store_many("vault_chunk", items)
    assert stored == 70
    assert [len(call) for call in provider.calls] == [32, 32, 6]
    rows = db.fetch_all("SELECT id FROM vec_map WHERE kind='vault_chunk'")
    assert len(rows) == 70


async def test_embed_and_store_many_is_a_noop_without_provider(db: Database) -> None:
    svc = EmbeddingService(db, None, dimensions=8)
    assert await svc.embed_and_store_many("vault_chunk", [(1, "a"), (2, "b")]) == 0


async def test_embed_and_store_many_survives_an_unreachable_provider(db: Database) -> None:
    provider = FakeProvider(fail=True)
    svc = EmbeddingService(db, provider, dimensions=8)
    assert await svc.embed_and_store_many("vault_chunk", [(1, "a")]) == 0


async def test_embed_and_store_many_skips_empty_texts(db: Database) -> None:
    provider = FakeProvider()
    svc = EmbeddingService(db, provider, dimensions=8)
    assert await svc.embed_and_store_many("vault_chunk", [(1, "  "), (2, "text")]) == 1
    assert provider.calls == [["text"]]

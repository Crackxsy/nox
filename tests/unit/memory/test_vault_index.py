"""nox.memory.vault_index: incremental indexing, privacy-zone exclusion, `nox: ignore`, deletions
(ST-07-03 AC1-AC5)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from nox.data.db import Database
from nox.memory.vault_index import VaultIndexer


class FakeZones:
    def __init__(self, zoned_substrings: tuple[str, ...] = ("secret",)) -> None:
        self._zoned = zoned_substrings

    def path_zone(self, path: str) -> str | None:
        normalized = path.replace("\\", "/")
        for needle in self._zoned:
            if needle in normalized:
                return "test-zone"
        return None


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "nox.db")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    v.mkdir()
    return v


async def test_full_scan_indexes_notes_and_chunks(db: Database, vault: Path) -> None:
    (vault / "a.md").write_text(
        "---\ntitle: A\n---\n\n# A\n\nSome content about kill switch.\n", encoding="utf-8"
    )
    idx = VaultIndexer(db, vault)
    stats = await idx.full_scan()
    assert stats.indexed == 1
    row = db.fetch_one("SELECT * FROM vault_index WHERE path = 'a.md'")
    assert row is not None
    assert row["title"] == "A"
    chunks = db.fetch_all("SELECT * FROM vault_chunks WHERE path = 'a.md'")
    assert len(chunks) == 1


async def test_rescan_unchanged_note_is_noop(db: Database, vault: Path) -> None:
    (vault / "a.md").write_text("---\ntitle: A\n---\n\nbody\n", encoding="utf-8")
    idx = VaultIndexer(db, vault)
    await idx.full_scan()
    stats2 = await idx.full_scan()
    assert stats2.indexed == 0
    assert stats2.unchanged == 1


async def test_nox_ignore_frontmatter_excluded(db: Database, vault: Path) -> None:
    (vault / "private.md").write_text(
        "---\nnox: { ignore: true }\n---\n\nsecret stuff\n", encoding="utf-8"
    )
    idx = VaultIndexer(db, vault)
    stats = await idx.full_scan()
    assert stats.zoned_skipped == 1
    assert db.fetch_one("SELECT * FROM vault_index WHERE path = 'private.md'") is None


async def test_privacy_zone_path_excluded(db: Database, vault: Path) -> None:
    (vault / "secret").mkdir()
    (vault / "secret" / "bank.md").write_text(
        "---\ntitle: Bank\n---\n\nbanking info\n", encoding="utf-8"
    )
    idx = VaultIndexer(db, vault, zones=FakeZones())
    stats = await idx.full_scan()
    assert stats.zoned_skipped == 1
    assert db.fetch_all("SELECT * FROM vault_index") == []
    assert db.fetch_all("SELECT * FROM vault_chunks") == []


async def test_note_becoming_zoned_is_scrubbed(db: Database, vault: Path) -> None:
    path = vault / "was_public.md"
    path.write_text("---\ntitle: X\n---\n\ncontent\n", encoding="utf-8")
    idx = VaultIndexer(db, vault)
    await idx.full_scan()
    assert db.fetch_one("SELECT * FROM vault_index WHERE path='was_public.md'") is not None

    zoning_idx = VaultIndexer(db, vault, zones=FakeZones(("was_public",)))
    outcome = await zoning_idx.index_path(path)
    assert outcome.action == "zoned"
    assert db.fetch_one("SELECT * FROM vault_index WHERE path='was_public.md'") is None


async def test_deleted_file_removed_from_index(db: Database, vault: Path) -> None:
    path = vault / "gone.md"
    path.write_text("---\ntitle: Gone\n---\n\nbody\n", encoding="utf-8")
    idx = VaultIndexer(db, vault)
    await idx.full_scan()
    assert db.fetch_one("SELECT * FROM vault_index WHERE path='gone.md'") is not None
    path.unlink()
    stats = await idx.full_scan()
    assert stats.removed == 1
    assert db.fetch_one("SELECT * FROM vault_index WHERE path='gone.md'") is None


async def test_vault_wins_conflict_reindex_overwrites_row(db: Database, vault: Path) -> None:
    """A vault-owned note and a stale index disagreeing resolves in the vault's favour on rescan
    (Data Model conflict rule, NFR-6): the index row's hash/title always reflect the file on
    disk."""
    path = vault / "note.md"
    path.write_text("---\ntitle: Old\n---\n\nold body\n", encoding="utf-8")
    idx = VaultIndexer(db, vault)
    await idx.full_scan()
    path.write_text("---\ntitle: New\n---\n\nnew body\n", encoding="utf-8")
    await idx.full_scan()
    row = db.fetch_one("SELECT * FROM vault_index WHERE path='note.md'")
    assert row is not None
    assert row["title"] == "New"


async def test_vault_unreachable_returns_empty_stats(db: Database, tmp_path: Path) -> None:
    idx = VaultIndexer(db, tmp_path / "does-not-exist")
    stats = await idx.full_scan()
    assert stats.scanned == 0


class CountingEmbeddings:
    """Records how the indexer calls the embedding service (one request per note, not per chunk)."""

    def __init__(self) -> None:
        self.batches: list[list[tuple[int, str]]] = []
        self.removed: list[int] = []

    async def embed_and_store_many(
        self, kind: str, items: list[tuple[int, str]], **_kwargs: object
    ) -> int:
        assert kind == "vault_chunk"
        self.batches.append(list(items))
        return len(items)

    def remove(self, _kind: str, ref_id: int) -> None:
        self.removed.append(ref_id)


async def test_chunks_of_one_note_are_embedded_in_a_single_batch(db: Database, vault: Path) -> None:
    """The first scan sent one request per chunk; `/api/embed` takes the whole note at once."""
    body = "\n\n".join(f"## Section {i}\n\n" + ("lorem ipsum " * 300) for i in range(6))
    (vault / "long.md").write_text(f"---\ntitle: Long\n---\n\n{body}\n", encoding="utf-8")
    embeddings = CountingEmbeddings()
    idx = VaultIndexer(db, vault, embeddings=embeddings)  # type: ignore[arg-type]
    await idx.full_scan()

    chunks = db.fetch_all("SELECT id FROM vault_chunks WHERE path = 'long.md'")
    assert len(chunks) > 1  # the note really is chunked
    assert len(embeddings.batches) == 1
    assert [cid for cid, _text in embeddings.batches[0]] == [int(r["id"]) for r in chunks]


async def test_rescan_only_embeds_the_chunks_that_changed(db: Database, vault: Path) -> None:
    note = vault / "a.md"
    note.write_text("---\ntitle: A\n---\n\nfirst body\n", encoding="utf-8")
    embeddings = CountingEmbeddings()
    idx = VaultIndexer(db, vault, embeddings=embeddings)  # type: ignore[arg-type]
    await idx.full_scan()
    assert len(embeddings.batches) == 1

    await idx.full_scan()  # unchanged: no request at all
    assert len(embeddings.batches) == 1

    note.write_text("---\ntitle: A\n---\n\nsecond body\n", encoding="utf-8")
    await idx.full_scan()
    assert len(embeddings.batches) == 2

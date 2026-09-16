"""Embedding service: `nomic-embed-text` via Ollama into sqlite-vec's `memory_vec`, with an honest
FTS5 fallback when Ollama is unreachable (ST-07-02, Data Model L2, SP-07).

Callers never branch on availability: `search()` tries the vector path first and transparently
falls back to FTS5, returning `limited=True` so the caller can be honest about degraded search
rather than silently pretending vectors were used (Runtime Lifecycle "never fake availability").

Writes come in two shapes: `embed_and_store` for a single text, and `embed_and_store_many` for a
whole note's chunks - one `/api/embed` request per `MAX_EMBED_BATCH` texts instead of one per
chunk, with the sqlite-vec upserts for each batch done in one `asyncio.to_thread` pass (bulk work
off the loop, which the connection lock makes safe - see `nox.data.db`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from nox.core.logging import get_logger
from nox.data.db import Database

log = get_logger(__name__)

Kind = Literal["memory_item", "vault_chunk"]

#: Texts per `/api/embed` request. Batching the first vault scan per note turned ~1 request per
#: chunk into one per note; the cap keeps a single request (and its timeout) bounded.
MAX_EMBED_BATCH = 32


class EmbedProvider(Protocol):
    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]: ...


@dataclass(frozen=True, slots=True)
class SearchHit:
    kind: Kind
    ref_id: int
    score: float  # higher = more relevant, whichever path produced it
    via: Literal["vector", "fts"]


class EmbeddingService:
    """Owns `vec_map`/`memory_vec` writes and the vector/FTS5 search fallback.

    `provider is None` (no Ollama configured) behaves exactly like a failed probe: every write is a
    silent no-op (FTS5 still indexes the row via 0001's triggers) and every search uses FTS5.
    """

    def __init__(
        self,
        db: Database,
        provider: EmbedProvider | None,
        *,
        dimensions: int = 768,
        model: str = "nomic-embed-text",
    ) -> None:
        self._db = db
        self._provider = provider
        self._dimensions = dimensions
        self._model = model
        self._vec_ready: bool | None = None  # None = not probed yet

    @property
    def model(self) -> str:
        return self._model

    async def available(self) -> bool:
        """Best-effort probe: sqlite-vec loads AND Ollama answers for one short text. Cached after
        the first call in each direction that succeeds; a prior failure is re-probed (Ollama may
        come back up while Nox keeps running)."""
        if self._provider is None:
            return False
        if not self._db.ensure_memory_vec(self._dimensions):
            return False
        try:
            vectors = await self._provider.embed(["ping"], model=self._model)
        except Exception as exc:  # noqa: BLE001 - unavailability is a health finding, not a crash
            log.info("memory.embed_probe_failed", error=f"{type(exc).__name__}: {exc}")
            return False
        return bool(vectors) and len(vectors[0]) == self._dimensions

    # ---- writes --------------------------------------------------------------------------------

    async def embed_and_store(self, kind: Kind, ref_id: int, text: str) -> bool:
        """Embed `text` and upsert it into `memory_vec` keyed via `vec_map`. Returns False (no
        exception) whenever vectors are unavailable - the FTS5 shadow table already has the row."""
        return await self.embed_and_store_many(kind, [(ref_id, text)]) == 1

    async def embed_and_store_many(
        self,
        kind: Kind,
        items: Sequence[tuple[int, str]],
        *,
        batch_size: int = MAX_EMBED_BATCH,
    ) -> int:
        """Embed `(ref_id, text)` pairs in batches of at most `batch_size` - one request per batch,
        one threaded DB pass per batch - and return how many vectors were stored. Like
        `embed_and_store` it never raises for unavailability: a failing batch stops the run and
        what was already stored is reported honestly."""
        if self._provider is None:
            return 0
        pending = [(ref_id, text) for ref_id, text in items if text.strip()]
        if not pending:
            return 0
        if not self._db.ensure_memory_vec(self._dimensions):
            return 0
        size = max(1, batch_size)
        stored = 0
        for start in range(0, len(pending), size):
            batch = pending[start : start + size]
            try:
                vectors = await self._provider.embed([text for _, text in batch], model=self._model)
            except Exception as exc:  # noqa: BLE001
                log.info("memory.embed_failed", kind=kind, count=len(batch), error=str(exc))
                return stored
            if len(vectors) != len(batch):
                log.warning("memory.embed_bad_batch", kind=kind, want=len(batch), got=len(vectors))
                return stored
            rows: list[tuple[int, bytes]] = []
            for (ref_id, _text), vector in zip(batch, vectors, strict=True):
                if len(vector) != self._dimensions:
                    log.warning("memory.embed_bad_shape", kind=kind, ref_id=ref_id)
                    continue
                rows.append((ref_id, _pack(vector)))
            if rows:
                await asyncio.to_thread(self._store_vectors, kind, rows)
                stored += len(rows)
        return stored

    def _store_vectors(self, kind: Kind, rows: list[tuple[int, bytes]]) -> None:
        """One transaction for a whole batch. Runs in a worker thread (`nox.data.db`: bulk work
        goes through `asyncio.to_thread`, the connection lock makes it safe)."""
        with self._db.transaction():
            for ref_id, blob in rows:
                vec_rowid = self._vec_rowid(kind, ref_id, create=True)
                self._db.execute("DELETE FROM memory_vec WHERE rowid = ?", (vec_rowid,))
                self._db.execute(
                    "INSERT INTO memory_vec (rowid, embedding) VALUES (?, ?)", (vec_rowid, blob)
                )

    def remove(self, kind: Kind, ref_id: int) -> None:
        vec_rowid = self._vec_rowid(kind, ref_id, create=False)
        if vec_rowid is None:
            return
        with self._db.transaction():
            if self._db.vec_loaded:
                self._db.execute("DELETE FROM memory_vec WHERE rowid = ?", (vec_rowid,))
            self._db.execute("DELETE FROM vec_map WHERE id = ?", (vec_rowid,))

    def _vec_rowid(self, kind: Kind, ref_id: int, *, create: bool) -> int | None:
        row = self._db.fetch_one(
            "SELECT id FROM vec_map WHERE kind = ? AND ref_id = ?", (kind, ref_id)
        )
        if row is not None:
            return int(row["id"])
        if not create:
            return None
        cur = self._db.execute("INSERT INTO vec_map (kind, ref_id) VALUES (?, ?)", (kind, ref_id))
        return int(cur.lastrowid or 0)

    # ---- search ----------------------------------------------------------------------------------

    async def search(self, query: str, *, k: int = 10, kind: Kind | None = None) -> list[SearchHit]:
        """Vector search when possible, else FTS5. Never raises for "no vectors" - only for a
        genuinely broken query (e.g. empty)."""
        query = query.strip()
        if not query:
            return []
        vector_hits = await self._search_vector(query, k=k, kind=kind)
        if vector_hits is not None:
            return vector_hits
        return self._search_fts(query, k=k, kind=kind)

    async def _search_vector(
        self, query: str, *, k: int, kind: Kind | None
    ) -> list[SearchHit] | None:
        if self._provider is None or not self._db.ensure_memory_vec(self._dimensions):
            return None
        try:
            vectors = await self._provider.embed([query], model=self._model)
        except Exception as exc:  # noqa: BLE001
            log.info("memory.search_embed_failed", error=str(exc))
            return None
        if not vectors or len(vectors[0]) != self._dimensions:
            return None
        blob = _pack(vectors[0])
        rows = self._db.fetch_all(
            "SELECT rowid, distance FROM memory_vec WHERE embedding MATCH ? "
            "ORDER BY distance LIMIT ?",
            (blob, k * 4 if kind else k),  # over-fetch when filtering by kind below
        )
        hits: list[SearchHit] = []
        for row in rows:
            mapped = self._db.fetch_one(
                "SELECT kind, ref_id FROM vec_map WHERE id = ?", (row["rowid"],)
            )
            if mapped is None:
                continue
            if kind is not None and mapped["kind"] != kind:
                continue
            distance = float(row["distance"])
            hits.append(
                SearchHit(
                    kind=mapped["kind"],
                    ref_id=int(mapped["ref_id"]),
                    score=1.0 / (1.0 + distance),
                    via="vector",
                )
            )
            if len(hits) >= k:
                break
        return hits

    def _search_fts(self, query: str, *, k: int, kind: Kind | None) -> list[SearchHit]:
        hits: list[SearchHit] = []
        fts_query = _fts_escape(query)
        if kind in (None, "memory_item"):
            for row in self._db.fetch_all(
                "SELECT m.id AS id, bm25(memory_items_fts) AS rank FROM memory_items m "
                "JOIN memory_items_fts f ON f.rowid = m.id WHERE memory_items_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (fts_query, k),
            ):
                hits.append(
                    SearchHit(
                        kind="memory_item",
                        ref_id=int(row["id"]),
                        score=-float(row["rank"]),
                        via="fts",
                    )
                )
        if kind in (None, "vault_chunk"):
            for row in self._db.fetch_all(
                "SELECT c.id AS id, bm25(vault_chunks_fts) AS rank FROM vault_chunks c "
                "JOIN vault_chunks_fts f ON f.rowid = c.id WHERE vault_chunks_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (fts_query, k),
            ):
                hits.append(
                    SearchHit(
                        kind="vault_chunk",
                        ref_id=int(row["id"]),
                        score=-float(row["rank"]),
                        via="fts",
                    )
                )
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]


def _pack(vector: list[float]) -> bytes:
    import struct

    return struct.pack(f"{len(vector)}f", *vector)


def _fts_escape(query: str) -> str:
    """FTS5 MATCH treats punctuation specially; quote each token so a query like "what's next"
    doesn't raise a syntax error."""
    tokens = [t for t in query.replace('"', " ").split() if t]
    return " ".join(f'"{t}"' for t in tokens) or '""'

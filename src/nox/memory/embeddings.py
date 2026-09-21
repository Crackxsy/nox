"""Embedding service: `nomic-embed-text` via Ollama into sqlite-vec's `memory_vec`, with an honest
FTS5 fallback when Ollama is unreachable.

Callers never branch on availability: `search` tries the vector path first and transparently falls
back to FTS5, returning `limited=True` so the caller can be honest about degraded search rather
than silently pretending vectors were used.

Writes come in two shapes: `embed_and_store` for a single text, and `embed_and_store_many` for a
whole note's chunks - one `/api/embed` request per `MAX_EMBED_BATCH` texts instead of one per
chunk, with the sqlite-vec upserts for each batch done in one `asyncio.to_thread` pass (bulk work
off the loop, which the connection lock makes safe - see `nox.data.db`).

Scoring: vectors are stored unit-normalised, so sqlite-vec's L2 distance converts exactly to
cosine similarity (`_cosine`). A caller can therefore compare a hit against a fixed threshold and
mean something by it - on the benchmark corpus an on-topic chunk scores 0.75-0.82 and an unrelated
one 0.38-0.62, a spread the previous `1 / (1 + distance)` mapping squeezed into 0.47-0.62.
"""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
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

#: Query embeddings kept in memory (LRU). An embedding is a pure function of (model, text), so a
#: repeated question can reuse one instead of paying another round trip to the model server
#: (measured: ~40 ms warm, ~1500 ms when the embedding model has to be loaded first). Only search
#: queries are cached - the write path embeds each text once anyway. At 768 float32 dimensions one
#: entry is ~3 KB, so the whole cache is well under a megabyte.
QUERY_CACHE_SIZE = 256

#: How far an embedding's length may be from 1.0 before `_pack` normalises it. `nomic-embed-text`
#: already returns unit vectors, so this is a no-op for the current model and the stored vectors of
#: existing installations stay valid; it is what makes the cosine score correct for a model that
#: does not.
_NORM_TOLERANCE = 1e-6


class EmbedProvider(Protocol):
    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]: ...


@dataclass(frozen=True, slots=True)
class SearchHit:
    kind: Kind
    ref_id: int
    #: For `via="vector"`: cosine similarity in [-1, 1], where 1 is identical text. For
    #: `via="fts"`: the negated BM25 rank, which is an open-ended scale with no comparable
    #: meaning - never compare the two against one threshold.
    score: float
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
        query_cache_size: int = QUERY_CACHE_SIZE,
    ) -> None:
        self._db = db
        self._provider = provider
        self._dimensions = dimensions
        self._model = model
        self._vec_ready: bool | None = None  # None = not probed yet
        self._query_cache: OrderedDict[str, bytes] = OrderedDict()
        self._query_cache_size = max(0, query_cache_size)

    @property
    def model(self) -> str:
        return self._model

    def set_query_cache_size(self, size: int) -> None:
        """Resize the query-embedding cache; 0 turns it off (a benchmark's baseline run)."""
        self._query_cache_size = max(0, size)
        while len(self._query_cache) > self._query_cache_size:
            self._query_cache.popitem(last=False)

    def clear_query_cache(self) -> None:
        """Drop the cached query embeddings, e.g. after the embedding model changed."""
        self._query_cache.clear()

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
            except Exception as exc:  # noqa: BLE001 - any provider failure degrades to FTS only
                log.warning("memory.embed_failed", kind=kind, count=len(batch), error=str(exc))
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
        blob = await self._query_vector(self._provider, query)
        if blob is None:
            return None
        return await asyncio.to_thread(self._vector_hits, blob, k=k, kind=kind)

    async def _query_vector(self, provider: EmbedProvider, query: str) -> bytes | None:
        """The packed embedding of a search query, from the LRU cache or the provider."""
        key = " ".join(query.split()).casefold()
        cached = self._query_cache.get(key)
        if cached is not None:
            self._query_cache.move_to_end(key)
            return cached
        try:
            vectors = await provider.embed([query], model=self._model)
        except Exception as exc:  # noqa: BLE001 - any provider failure degrades to FTS only
            log.warning("memory.search_embed_failed", error=str(exc))
            return None
        if not vectors or len(vectors[0]) != self._dimensions:
            return None
        blob = _pack(vectors[0])
        if self._query_cache_size:
            self._query_cache[key] = blob
            while len(self._query_cache) > self._query_cache_size:
                self._query_cache.popitem(last=False)
        return blob

    def _vector_hits(self, blob: bytes, *, k: int, kind: Kind | None) -> list[SearchHit]:
        """The sqlite half of a vector search, in two queries and one worker thread.

        sqlite-vec wants its k-nearest-neighbour query on its own, so the mapping rows are then
        fetched for the whole result set at once rather than one round trip per hit.
        """
        rows = self._db.fetch_all(
            "SELECT rowid, distance FROM memory_vec WHERE embedding MATCH ? "
            "ORDER BY distance LIMIT ?",
            (blob, k * 4 if kind else k),  # over-fetch when filtering by kind below
        )
        if not rows:
            return []
        rowids = [int(row["rowid"]) for row in rows]
        placeholders = ",".join("?" * len(rowids))
        mapped_rows = self._db.fetch_all(
            f"SELECT id, kind, ref_id FROM vec_map WHERE id IN ({placeholders})",  # noqa: S608
            tuple(rowids),
        )
        mapping = {int(row["id"]): (row["kind"], int(row["ref_id"])) for row in mapped_rows}
        hits: list[SearchHit] = []
        for row in rows:
            mapped = mapping.get(int(row["rowid"]))
            if mapped is None:
                continue
            hit_kind, ref_id = mapped
            if kind is not None and hit_kind != kind:
                continue
            hits.append(
                SearchHit(
                    kind=hit_kind,
                    ref_id=ref_id,
                    score=_cosine(float(row["distance"])),
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
    """Pack an embedding for `memory_vec`, unit-normalised first (module docstring: scoring)."""
    import struct  # noqa: PLC0415 - only the packing helpers need it

    length = math.sqrt(sum(x * x for x in vector))
    if length > 0.0 and abs(length - 1.0) > _NORM_TOLERANCE:
        vector = [x / length for x in vector]
    return struct.pack(f"{len(vector)}f", *vector)


def _cosine(distance: float) -> float:
    """Cosine similarity from the squared-free L2 distance between two unit vectors.

    For unit vectors ``|a - b|^2 = 2 - 2 cos(a, b)``, so the whole [-1, 1] range survives instead
    of being squeezed into the narrow band a ``1 / (1 + distance)`` mapping produces. That matters:
    the useful decisions here are threshold decisions, and a threshold needs a spread scale.
    """
    return max(-1.0, min(1.0, 1.0 - (distance * distance) / 2.0))


def _fts_escape(query: str) -> str:
    """FTS5 MATCH treats punctuation specially; quote each token so a query like "what's next"
    doesn't raise a syntax error."""
    tokens = [t for t in query.replace('"', " ").split() if t]
    return " ".join(f'"{t}"' for t in tokens) or '""'

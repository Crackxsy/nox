"""Retrieval service: `retrieve(query, k)` -> ranked memory items + vault chunks for prompt context
(fast path). Hard token cap on the returned context (`max_tokens`, ~4 chars/token); honest
`found=False` when nothing relevant exists rather than inventing an answer.

Two properties of the caller decide the shape of this module. First, the result goes into the
system prompt of a small local model, where every retrieved token is measurable latency: on
`llama3.2:3b` an extra 1000 tokens of context cost about 2.2 s before the first token, because
context changes per turn and therefore defeats the server's prefix cache. Second, an irrelevant
chunk is worse than no chunk - it is what makes a small model answer confidently about the wrong
thing. Hence `min_score`: hits below it are dropped instead of padding the prompt.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from nox.data.db import Database
from nox.memory.embeddings import EmbeddingService, SearchHit

#: Context budget for one turn. Measured on `llama3.2:3b`: ~2.2 ms of time-to-first-token per
#: prompt token that is not already in the server's prefix cache, so 600 tokens is about 1.3 s in
#: the worst case and still holds the three or four chunks that actually answer a question.
DEFAULT_MAX_TOKENS = 600
CHARS_PER_TOKEN = 4  # rough estimate; good enough for a hard budget cap, not exact tokenization

#: Cosine similarity below which a vector hit is not worth a prompt token. Three bands are
#: measurable on the benchmark corpus (`scripts/bench_chat.py`): a chunk that actually answers the
#: question scores 0.79-0.82, a note on a neighbouring topic 0.60-0.74, and small talk ("Hallo",
#: "Danke") 0.43-0.45. The floor sits above the middle band on purpose - a note about backups does
#: not help answer a question about backups going wrong, it only costs tokens and tempts a small
#: model to answer from it. Lower it in `memory.retrieval_min_score` for a vault whose notes are
#: long and diffuse. Keyword (FTS5) hits score on a different, unbounded scale and are never
#: filtered by this value - the `limited` flag already says the result is degraded.
DEFAULT_MIN_SCORE = 0.75

#: How far below the best hit a further hit may score and still be included. A question usually has
#: one chunk that answers it; the next-best chunk of a small vault is "the rest of the vault", and
#: including it costs latency and invites the model to answer from the wrong note.
DEFAULT_SCORE_MARGIN = 0.08


@dataclass(frozen=True, slots=True)
class RetrievedItem:
    kind: Literal["memory_item", "vault_chunk"]
    ref_id: int
    score: float
    text: str
    source: str  # vault path, or the memory item's `source` field
    via: Literal["vector", "fts"]


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    query: str
    items: list[RetrievedItem] = field(default_factory=list)
    found: bool = False
    limited: bool = False  # True when any hit came from the FTS5 fallback, not vectors
    latency_ms: float = 0.0
    #: Hits dropped for being below `min_score`, so the caller can say "nothing relevant" honestly
    #: instead of "nothing found".
    dropped: int = 0

    @property
    def relevance(self) -> float:
        """Best *semantic* score in the result, or 0.0 when there is none.

        Keyword hits score on an unrelated scale, so they count as "relevance unknown" (0.0)
        rather than being compared against vector scores.
        """
        scores = [item.score for item in self.items if item.via == "vector"]
        return max(scores) if scores else 0.0


class RetrievalService:
    def __init__(
        self,
        db: Database,
        embeddings: EmbeddingService,
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        chars_per_token: int = CHARS_PER_TOKEN,
        min_score: float = DEFAULT_MIN_SCORE,
        score_margin: float = DEFAULT_SCORE_MARGIN,
    ) -> None:
        self._db = db
        self._embeddings = embeddings
        self._chars_per_token = chars_per_token
        self._budget_chars = max_tokens * chars_per_token
        self._min_score = min_score
        self._score_margin = score_margin

    @property
    def max_tokens(self) -> int:
        return self._budget_chars // self._chars_per_token

    def set_budget(self, *, max_tokens: int, min_score: float, score_margin: float) -> None:
        """Re-tune the prompt-cost knobs at runtime (the latency benchmark's baseline mode).

        A `score_margin` of 0.0 keeps only the single best hit; 2.0 spans the whole cosine range
        and therefore switches the margin off.
        """
        self._budget_chars = max_tokens * self._chars_per_token
        self._min_score = min_score
        self._score_margin = score_margin

    async def retrieve(self, query: str, *, k: int = 8) -> RetrievalResult:
        started = time.perf_counter()
        query = query.strip()
        if not query:
            return RetrievalResult(query=query, latency_ms=0.0)
        hits = await self._embeddings.search(query, k=k)
        floor = self._vector_floor(hits)
        items: list[RetrievedItem] = []
        used = 0
        limited = False
        dropped = 0
        for hit in hits:
            if hit.via == "vector" and hit.score < floor:
                dropped += 1
                continue
            if hit.via == "fts":
                limited = True
            text, source = self._hydrate(hit.kind, hit.ref_id)
            if text is None:
                continue
            if items and used + len(text) > self._budget_chars:
                break
            used += len(text)
            items.append(
                RetrievedItem(
                    kind=hit.kind,
                    ref_id=hit.ref_id,
                    score=hit.score,
                    text=text,
                    source=source or "",
                    via=hit.via,
                )
            )
        latency_ms = (time.perf_counter() - started) * 1000
        return RetrievalResult(
            query=query,
            items=items,
            found=bool(items),
            limited=limited,
            latency_ms=latency_ms,
            dropped=dropped,
        )

    def _vector_floor(self, hits: Sequence[SearchHit]) -> float:
        """The score a vector hit has to reach: the absolute floor, raised by the margin below the
        best hit when there is one (see `DEFAULT_MIN_SCORE` and `DEFAULT_SCORE_MARGIN`)."""
        scores = [hit.score for hit in hits if hit.via == "vector"]
        if not scores:
            return self._min_score
        return max(self._min_score, max(scores) - self._score_margin)

    def _hydrate(self, kind: str, ref_id: int) -> tuple[str | None, str | None]:
        if kind == "memory_item":
            row = self._db.fetch_one(
                "SELECT text, source FROM memory_items WHERE id = ?", (ref_id,)
            )
            if row is None:
                return None, None
            return str(row["text"]), str(row["source"] or "memory")
        row = self._db.fetch_one("SELECT text, path FROM vault_chunks WHERE id = ?", (ref_id,))
        if row is None:
            return None, None
        return str(row["text"]), str(row["path"])


def format_context(result: RetrievalResult) -> str:
    """Render a `RetrievalResult` as a compact prompt block with source citations (dashboard
    sources panel reads the structured `RetrievalResult` directly; this is for the system
    prompt)."""
    if not result.items:
        return ""
    lines = []
    for item in result.items:
        tag = "vault" if item.kind == "vault_chunk" else "memory"
        lines.append(f"[{tag}:{item.source}#{item.ref_id} score={item.score:.2f}] {item.text}")
    prefix = "(degraded: keyword search only)\n" if result.limited else ""
    return prefix + "\n\n".join(lines)

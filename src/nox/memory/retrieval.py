"""Retrieval service: `retrieve(query, k)` -> ranked memory items + vault chunks for prompt context
(ST-07-05 fast path). Hard token cap on the returned context (`max_tokens`, ~4 chars/token); honest
`found=False` when nothing relevant exists rather than inventing an answer (FR-8.2)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

from nox.data.db import Database
from nox.memory.embeddings import EmbeddingService

DEFAULT_MAX_TOKENS = 4000
CHARS_PER_TOKEN = 4  # rough estimate; good enough for a hard budget cap, not exact tokenization


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


class RetrievalService:
    def __init__(
        self,
        db: Database,
        embeddings: EmbeddingService,
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        chars_per_token: int = CHARS_PER_TOKEN,
    ) -> None:
        self._db = db
        self._embeddings = embeddings
        self._budget_chars = max_tokens * chars_per_token

    async def retrieve(self, query: str, *, k: int = 8) -> RetrievalResult:
        started = time.perf_counter()
        query = query.strip()
        if not query:
            return RetrievalResult(query=query, latency_ms=0.0)
        hits = await self._embeddings.search(query, k=k)
        items: list[RetrievedItem] = []
        used = 0
        limited = False
        for hit in hits:
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
            query=query, items=items, found=bool(items), limited=limited, latency_ms=latency_ms
        )

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

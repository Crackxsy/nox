"""Markdown chunking for vault indexing: splits a note body into retrieval-sized chunks along
heading/paragraph boundaries, each with a stable content hash so the indexer can diff unchanged
chunks instead of re-embedding everything on every save."""

from __future__ import annotations

from dataclasses import dataclass

from nox.memory.frontmatter import hash_text

MAX_CHARS = 1200
MIN_CHARS = 40


@dataclass(frozen=True, slots=True)
class Chunk:
    ord: int
    text: str
    hash: str


def chunk_text(body: str, *, max_chars: int = MAX_CHARS) -> list[Chunk]:
    """Split on blank-line paragraph boundaries, then greedily pack paragraphs (and, for a heading,
    start a new chunk) up to `max_chars`. A single paragraph longer than `max_chars` is kept whole
    rather than cut mid-sentence (chunks are for retrieval context, not a hard token budget)."""
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buffer = ""
    for para in paragraphs:
        starts_heading = para.startswith("#")
        if buffer and (starts_heading or len(buffer) + 2 + len(para) > max_chars):
            chunks.append(buffer)
            buffer = para
        else:
            buffer = f"{buffer}\n\n{para}" if buffer else para
    if buffer:
        chunks.append(buffer)
    # Merge chunks that ended up too small (e.g. a lone trailing heading) into the previous one.
    merged: list[str] = []
    for text in chunks:
        if merged and len(text) < MIN_CHARS:
            merged[-1] = f"{merged[-1]}\n\n{text}"
        else:
            merged.append(text)
    return [Chunk(ord=i, text=text, hash=hash_text(text)) for i, text in enumerate(merged)]

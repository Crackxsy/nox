"""Vault indexer: incremental `vault_index`/`vault_chunks` maintenance (ST-07-03, Data Model L3).

Privacy zones outrank everything (FR-7.16): a note under `nox: { ignore: true }` or a
`privacy.zones` path is never written to `vault_index`, `vault_chunks`, or `memory_vec` - checked
before any content leaves the file, not filtered out afterward. The vault is the source of truth;
`full_scan()` is always safe to re-run and converges the index to exactly what full_scan would
produce from scratch (SP-13 "drift" contract).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from nox.core.logging import get_logger
from nox.data.db import Database
from nox.memory.chunking import Chunk, chunk_text
from nox.memory.embeddings import EmbeddingService
from nox.memory.frontmatter import is_ignored, parse_note

log = get_logger(__name__)


class ZoneMatcher(Protocol):
    def path_zone(self, path: str) -> str | None: ...


class IndexUpdatedHook(Protocol):
    async def __call__(self, path: str, chunk_count: int) -> None: ...


@dataclass(slots=True)
class ScanStats:
    scanned: int = 0
    indexed: int = 0
    unchanged: int = 0
    zoned_skipped: int = 0
    removed: int = 0


@dataclass
class IndexOutcome:
    path: Path
    action: str  # "indexed" | "unchanged" | "zoned" | "removed" | "error"
    detail: str = ""


def _posix(path: Path) -> str:
    return path.as_posix()


class VaultIndexer:
    def __init__(
        self,
        db: Database,
        vault_dir: Path,
        *,
        zones: ZoneMatcher | None = None,
        embeddings: EmbeddingService | None = None,
        scan_yield_s: float = 0.01,
        on_index_updated: IndexUpdatedHook | None = None,
    ) -> None:
        self._db = db
        self._vault_dir = Path(vault_dir)
        self._zones = zones
        self._embeddings = embeddings
        # A scan over an already-indexed vault has no real suspension point (unchanged notes
        # never await I/O), so it must yield explicitly or it starves heartbeats and IPC
        # handshakes (2026-09-15).
        self._scan_yield_s = scan_yield_s
        self._on_index_updated = on_index_updated

    # ---- single-file path (also used by the watcher) --------------------------------------------

    async def index_path(self, path: Path) -> IndexOutcome:
        # Deliberately synchronous pathlib/file I/O (ASYNC240): local-disk-only, same trade-off as
        # `Database` (module docstring) and `nox.pm.vault_repo.read_note` - a note is at most a few
        # hundred KB and there is no event loop contention worth an executor hop for this.
        if not path.is_file():  # noqa: ASYNC240
            return await self.remove_path(path)
        rel = self._rel(path)
        if rel is None:
            return IndexOutcome(path, "error", "outside vault_dir")
        if self._is_zoned(path, rel):
            # A previously-indexed note that just moved into a zone (or gained `nox: ignore`) must
            # be scrubbed, not merely skipped (FR-7.16).
            await self.remove_path(path)
            return IndexOutcome(path, "zoned")

        try:
            text = path.read_text(encoding="utf-8")  # noqa: ASYNC240
        except OSError as exc:
            return IndexOutcome(path, "error", str(exc))
        note = parse_note(path, text)
        if is_ignored(note):
            await self.remove_path(path)
            return IndexOutcome(path, "zoned", "nox: ignore")

        mtime = path.stat().st_mtime  # noqa: ASYNC240
        existing = self._db.fetch_one("SELECT * FROM vault_index WHERE path = ?", (rel,))
        if existing is not None and existing["hash"] == note.raw_hash:
            return IndexOutcome(path, "unchanged")

        title = str(note.frontmatter.get("title") or _guess_title(note.body) or path.stem)
        note_type = str(note.frontmatter.get("type") or "")
        tags = note.frontmatter.get("tags") or []
        if not isinstance(tags, list):
            tags = [str(tags)]
        indexed_at = _now_iso()

        with self._db.transaction():
            self._db.execute(
                "INSERT INTO vault_index (path, mtime, hash, title, type, tags_json, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, hash=excluded.hash, "
                "title=excluded.title, type=excluded.type, tags_json=excluded.tags_json, "
                "indexed_at=excluded.indexed_at",
                (
                    rel,
                    mtime,
                    note.raw_hash,
                    title,
                    note_type,
                    json.dumps([str(t) for t in tags]),
                    indexed_at,
                ),
            )
            old_chunks = {
                int(r["ord"]): (int(r["id"]), str(r["hash"]))
                for r in self._db.fetch_all(
                    "SELECT id, ord, hash FROM vault_chunks WHERE path = ?", (rel,)
                )
            }
        new_chunks = chunk_text(note.body)
        keep_ords = set()
        changed: list[Chunk] = []
        for chunk in new_chunks:
            keep_ords.add(chunk.ord)
            prior = old_chunks.get(chunk.ord)
            if prior is None or prior[1] != chunk.hash:  # unchanged chunks are not re-embedded
                changed.append(chunk)
            await asyncio.sleep(0)  # at most one chunk per loop iteration
        if changed:
            # One threaded DB pass and one embedding request per note instead of per chunk: the
            # first scan of the vault spent its time in per-chunk round trips (2026-09-15).
            pending = await asyncio.to_thread(self._store_chunks, rel, changed)
            if self._embeddings is not None and pending:
                await self._embeddings.embed_and_store_many("vault_chunk", pending)
        # Drop chunks beyond the new count (note got shorter).
        for ord_, (chunk_id, _hash) in old_chunks.items():
            if ord_ not in keep_ords:
                self._db.execute("DELETE FROM vault_chunks WHERE id = ?", (chunk_id,))
                if self._embeddings is not None:
                    self._embeddings.remove("vault_chunk", chunk_id)

        if self._on_index_updated is not None:
            await self._on_index_updated(rel, len(new_chunks))
        return IndexOutcome(path, "indexed")

    def _store_chunks(self, rel: str, chunks: list[Chunk]) -> list[tuple[int, str]]:
        """Upsert a note's changed chunks in one transaction and return `(chunk_id, text)` for
        each. Bulk DB work, so callers run it through `asyncio.to_thread` (`nox.data.db`)."""
        pending: list[tuple[int, str]] = []
        with self._db.transaction():
            for chunk in chunks:
                self._db.execute(
                    "INSERT INTO vault_chunks (path, ord, text, hash) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(path, ord) DO UPDATE SET text=excluded.text, hash=excluded.hash",
                    (rel, chunk.ord, chunk.text, chunk.hash),
                )
                row = self._db.fetch_one(
                    "SELECT id FROM vault_chunks WHERE path = ? AND ord = ?", (rel, chunk.ord)
                )
                if row is not None:
                    pending.append((int(row["id"]), chunk.text))
        return pending

    async def remove_path(self, path: Path) -> IndexOutcome:
        rel = self._rel(path)
        if rel is None:
            return IndexOutcome(path, "error", "outside vault_dir")
        chunk_ids = [
            int(r["id"])
            for r in self._db.fetch_all("SELECT id FROM vault_chunks WHERE path = ?", (rel,))
        ]
        with self._db.transaction():
            self._db.execute("DELETE FROM vault_index WHERE path = ?", (rel,))
            self._db.execute("DELETE FROM vault_chunks WHERE path = ?", (rel,))  # belt + FK cascade
        if self._embeddings is not None:
            for chunk_id in chunk_ids:
                self._embeddings.remove("vault_chunk", chunk_id)
        return IndexOutcome(path, "removed")

    # ---- full scan -----------------------------------------------------------------------------

    async def full_scan(self) -> ScanStats:
        if not self._vault_dir.is_dir():
            log.warning("memory.vault_unreachable", vault_dir=str(self._vault_dir))
            return ScanStats()
        stats = ScanStats()
        seen: set[str] = set()
        for path in sorted(self._vault_dir.rglob("*.md")):
            stats.scanned += 1
            rel = self._rel(path)
            if rel is not None:
                seen.add(rel)
            outcome = await self.index_path(path)
            await asyncio.sleep(self._scan_yield_s)
            if outcome.action == "indexed":
                stats.indexed += 1
            elif outcome.action == "unchanged":
                stats.unchanged += 1
            elif outcome.action == "zoned":
                stats.zoned_skipped += 1
        known = {str(r["path"]) for r in self._db.fetch_all("SELECT path FROM vault_index")}
        for stale in known - seen:
            await self.remove_path(self._vault_dir / stale)
            stats.removed += 1
        return stats

    # ---- helpers -------------------------------------------------------------------------------

    def _rel(self, path: Path) -> str | None:
        try:
            return _posix(path.resolve().relative_to(self._vault_dir.resolve()))
        except ValueError:
            return None

    def _is_zoned(self, path: Path, rel: str) -> bool:
        if self._zones is None:
            return False
        return (
            self._zones.path_zone(_posix(path)) is not None
            or self._zones.path_zone(rel) is not None
        )


def _guess_title(body: str) -> str | None:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or None
    return None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()

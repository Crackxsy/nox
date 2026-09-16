"""Vault writer: ownership-aware writes with rollback (ST-07-04, Data Model L3).

Ownership rule: a note with `owner: nox` may be rewritten directly; every other note is never
silently overwritten - Nox appends under a `## Nox` section instead. A write with no explicit,
Nox-owned target goes to `00 - Inbox` with `nox: { written: true, source, confidence }`
frontmatter. A concurrent edit (the file changed on disk since Nox last read it) never loses data:
both versions are kept, one as a `.conflict-<ts>.md` sibling. Every rewrite of an existing note
saves the prior content to `vault_note_versions` first, so `rollback()` can restore it (git
bundling is out of scope here - this vault is not a git repository, see ST-07-04 Notes in the
report).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from nox.data.db import Database
from nox.memory.frontmatter import VaultNote, owner, parse_note, render_note


class ZoneMatcher(Protocol):
    def path_zone(self, path: str) -> str | None: ...


NOX_SECTION_HEADING = "## Nox"


class VaultWriteRefusedError(RuntimeError):
    """A privacy zone or `allows_memory_write() is False` refused this write (never silently
    downgraded, per ST-07-01/ST-07-03)."""


@dataclass(frozen=True, slots=True)
class WriteResult:
    path: Path
    action: str  # "created" | "appended" | "rewritten" | "conflict"
    conflict_path: Path | None = None


class VaultWriter:
    def __init__(
        self,
        db: Database,
        vault_dir: Path,
        *,
        inbox_dir: str = "00 - Inbox",
        zones: ZoneMatcher | None = None,
    ) -> None:
        self._db = db
        self._vault_dir = Path(vault_dir)
        self._inbox_dir = self._vault_dir / inbox_dir
        self._zones = zones

    def _refuse_if_zoned(self, path: Path) -> None:
        if self._zones is not None and self._zones.path_zone(str(path)) is not None:
            raise VaultWriteRefusedError(f"write into a privacy-zoned path refused: {path}")

    # ---- new content, no explicit owned target --------------------------------------------------

    def write_inbox_note(
        self,
        title: str,
        body: str,
        *,
        source: str,
        confidence: float = 0.5,
        filename: str | None = None,
    ) -> WriteResult:
        self._refuse_if_zoned(self._inbox_dir)
        self._inbox_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y-%m-%d %H%M%S")
        name = filename or f"{stamp} {_slug(title)}.md"
        path = self._inbox_dir / name
        if path.exists():
            path = self._inbox_dir / f"{stamp}-{uuid.uuid4().hex[:6]} {_slug(title)}.md"
        frontmatter = {
            "title": title,
            "nox": {"written": True, "source": source, "confidence": round(confidence, 2)},
        }
        text = render_note(frontmatter, f"\n# {title}\n\n{body}\n")
        path.write_text(text, encoding="utf-8")
        return WriteResult(path=path, action="created")

    # ---- writes into an existing note -----------------------------------------------------------

    def append_or_rewrite(
        self,
        path: Path,
        content: str,
        *,
        source: str,
        expected_hash: str | None = None,
    ) -> WriteResult:
        """`expected_hash` is the hash the caller last read (e.g. from `vault_index.hash`); when the
        file on disk no longer matches it, this is a concurrent edit and both versions are kept."""
        self._refuse_if_zoned(path)
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(render_note({}, content), encoding="utf-8")
            return WriteResult(path=path, action="created")

        note = parse_note(path)
        if expected_hash is not None and note.raw_hash != expected_hash:
            conflict_path = self._conflict_path(path)
            conflict_body = (
                f"{content}\n\n<!-- nox: conflict copy, source={source}, "
                f"{datetime.now(UTC).isoformat()} -->\n"
            )
            conflict_path.write_text(conflict_body, encoding="utf-8")
            return WriteResult(path=path, action="conflict", conflict_path=conflict_path)

        if owner(note) == "nox":
            self._save_version(note)
            path.write_text(content, encoding="utf-8")
            return WriteResult(path=path, action="rewritten")

        new_body = _append_under_heading(note.body, content)
        path.write_text(render_note(note.frontmatter, new_body), encoding="utf-8")
        return WriteResult(path=path, action="appended")

    # ---- rollback --------------------------------------------------------------------------------

    def _save_version(self, note: VaultNote) -> None:
        rel = self._rel(note.path)
        self._db.execute(
            "INSERT INTO vault_note_versions (path, content, saved_at) VALUES (?, ?, ?)",
            (rel, note.raw_text, datetime.now(UTC).isoformat()),
        )

    def rollback(self, path: Path) -> bool:
        """Restore the most recently saved previous version of `path` (only notes this writer
        rewrote in place carry one; returns False when there is nothing to restore)."""
        rel = self._rel(path)
        row = self._db.fetch_one(
            "SELECT id, content FROM vault_note_versions WHERE path = ? ORDER BY id DESC LIMIT 1",
            (rel,),
        )
        if row is None:
            return False
        path.write_text(str(row["content"]), encoding="utf-8")
        self._db.execute("DELETE FROM vault_note_versions WHERE id = ?", (row["id"],))
        return True

    # ---- helpers -----------------------------------------------------------------------------

    def _rel(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self._vault_dir.resolve()).as_posix()
        except ValueError:
            return str(path)

    def _conflict_path(self, path: Path) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        return path.with_name(f"{path.stem}.conflict-{stamp}{path.suffix}")


def _append_under_heading(body: str, content: str) -> str:
    if NOX_SECTION_HEADING in body:
        return f"{body.rstrip()}\n\n{content.strip()}\n"
    return f"{body.rstrip()}\n\n{NOX_SECTION_HEADING}\n\n{content.strip()}\n"


_SLUG_RE = re.compile(r"[^\w\- ]+", re.UNICODE)


def _slug(title: str) -> str:
    cleaned = _SLUG_RE.sub("", title).strip()
    return cleaned[:80] or "note"

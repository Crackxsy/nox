"""nox.memory.vault_writer: ownership branching, Inbox frontmatter, rollback, concurrent-edit
conflict handling (ST-07-04 AC1-AC4)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from nox.data.db import Database
from nox.memory.frontmatter import parse_note
from nox.memory.vault_writer import VaultWriter, VaultWriteRefusedError


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


def test_write_inbox_note_has_provenance_frontmatter(db: Database, vault: Path) -> None:
    writer = VaultWriter(db, vault)
    result = writer.write_inbox_note(
        "Session Summary", "We discussed the kill switch.", source="sess-1", confidence=0.8
    )
    assert result.action == "created"
    assert result.path.parent.name == "00 - Inbox"
    note = parse_note(result.path)
    assert note.frontmatter["nox"] == {"written": True, "source": "sess-1", "confidence": 0.8}


def test_append_to_non_owned_note_appends_under_nox_heading(db: Database, vault: Path) -> None:
    path = vault / "user.md"
    path.write_text("---\ntitle: User\n---\nUser's own words.\n", encoding="utf-8")
    writer = VaultWriter(db, vault)
    result = writer.append_or_rewrite(path, "Nox observed something.", source="sess-1")
    assert result.action == "appended"
    text = path.read_text(encoding="utf-8")
    assert "User's own words." in text
    assert "## Nox" in text
    assert "Nox observed something." in text


def test_owned_note_is_rewritten_directly(db: Database, vault: Path) -> None:
    path = vault / "owned.md"
    path.write_text("---\nowner: nox\n---\noriginal\n", encoding="utf-8")
    writer = VaultWriter(db, vault)
    result = writer.append_or_rewrite(path, "brand new content", source="sess-1")
    assert result.action == "rewritten"
    assert path.read_text(encoding="utf-8") == "brand new content"


def test_rollback_restores_previous_version(db: Database, vault: Path) -> None:
    path = vault / "owned.md"
    path.write_text("---\nowner: nox\n---\noriginal\n", encoding="utf-8")
    writer = VaultWriter(db, vault)
    writer.append_or_rewrite(path, "replaced content", source="sess-1")
    assert path.read_text(encoding="utf-8") == "replaced content"
    assert writer.rollback(path) is True
    assert path.read_text(encoding="utf-8") == "---\nowner: nox\n---\noriginal\n"


def test_rollback_without_prior_version_returns_false(db: Database, vault: Path) -> None:
    writer = VaultWriter(db, vault)
    assert writer.rollback(vault / "never-written.md") is False


def test_concurrent_edit_keeps_both_versions(db: Database, vault: Path) -> None:
    path = vault / "user.md"
    path.write_text("---\ntitle: User\n---\noriginal\n", encoding="utf-8")
    writer = VaultWriter(db, vault)
    stale_hash = parse_note(path).raw_hash
    # Simulate a manual edit happening after the caller last read the note.
    path.write_text("---\ntitle: User\n---\nmanually edited\n", encoding="utf-8")
    result = writer.append_or_rewrite(
        path, "concurrent nox content", source="sess-2", expected_hash=stale_hash
    )
    assert result.action == "conflict"
    assert result.conflict_path is not None
    assert "manually edited" in path.read_text(encoding="utf-8")
    assert "concurrent nox content" in result.conflict_path.read_text(encoding="utf-8")


def test_write_refused_in_privacy_zone(db: Database, vault: Path) -> None:
    class AlwaysZoned:
        def path_zone(self, path: str) -> str | None:
            return "zone"

    writer = VaultWriter(db, vault, zones=AlwaysZoned())
    with pytest.raises(VaultWriteRefusedError):
        writer.write_inbox_note("Title", "body", source="sess-1")

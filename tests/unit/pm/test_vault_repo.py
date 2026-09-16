"""`nox.pm.vault_repo`: parse/write frontmatter, preserve unknown fields and body, field-level
validation errors, id-scheme acceptance."""

from __future__ import annotations

from pathlib import Path

import pytest

from nox.pm.models import WorkItemValidationError
from nox.pm.vault_repo import PmVaultRepo, VaultNoteError, read_note, write_note_fields
from tests.unit.pm.conftest import STORY_MISSING_ESTIMATE_NOTE


def test_read_epic_maps_known_fields(repo: PmVaultRepo) -> None:
    item = repo.read_epic(next(repo.iter_epic_notes()))
    assert item.id == "EPIC-13"
    assert item.kind == "epic"
    assert item.title == "EPIC-13 Test Epic"
    assert item.status == "proposed"
    assert item.priority == "P1"
    assert item.created == "2026-09-09"


def test_read_story_maps_known_fields(repo: PmVaultRepo) -> None:
    path = repo.stories_dir / "ST-13-01 Todo Story.md"
    item = repo.read_story(path)
    assert item.id == "ST-13-01"
    assert item.kind == "story"
    assert item.status == "todo"
    assert item.epic_id == "EPIC-13"
    assert item.estimate == "L"


def test_missing_required_field_rejected_with_field_name(
    vault_dir: Path, repo: PmVaultRepo
) -> None:
    broken = repo.stories_dir / "ST-13-09 Broken Story.md"
    broken.write_text(STORY_MISSING_ESTIMATE_NOTE, encoding="utf-8")
    with pytest.raises(WorkItemValidationError) as excinfo:
        repo.read_story(broken)
    assert excinfo.value.field == "estimate"


def test_unknown_kind_filename_rejected(repo: PmVaultRepo) -> None:
    bad = repo.stories_dir / "NOTASTORY-01 Bad.md"
    bad.write_text(
        '---\ntitle: "x"\ntype: story\nstatus: todo\nepic: EPIC-13\npriority: P1\n'
        "estimate: M\ncreated: 2026-09-09\nupdated: 2026-09-09\ntags: [nox]\n---\nbody\n",
        encoding="utf-8",
    )
    with pytest.raises(WorkItemValidationError) as excinfo:
        repo.read_story(bad)
    assert excinfo.value.field == "id"


def test_no_frontmatter_raises_vault_note_error(tmp_path: Path) -> None:
    note = tmp_path / "plain.md"
    note.write_text("# just a heading\n", encoding="utf-8")
    with pytest.raises(VaultNoteError):
        read_note(note)


def test_all_items_round_trips_fixture_vault_without_data_loss(repo: PmVaultRepo) -> None:
    items = repo.all_items()
    ids = {item.id for item in items}
    assert ids == {"PRJ-NOX", "EPIC-13", "ST-13-01", "ST-13-02"}


def test_prd_id_scheme_is_logged_not_rejected(
    repo: PmVaultRepo, caplog: pytest.LogCaptureFixture
) -> None:
    note_path = repo.stories_dir / "ST-13-05 Prd Scheme Story.md"
    note_path.write_text(
        '---\ntitle: "x"\ntype: story\nstatus: todo\nepic: EPIC-13\npriority: P1\n'
        "estimate: M\ncreated: 2026-09-09\nupdated: 2026-09-09\ntags: [nox]\n"
        "machine-data:\n  document_id: NOX-13.5\n---\nbody\n",
        encoding="utf-8",
    )
    item = repo.read_story(note_path)  # must not raise
    assert item.id == "ST-13-05"


# ---- write-back: field scoping, byte-identical otherwise --------------------------------------


def test_write_note_fields_noop_is_byte_identical(repo: PmVaultRepo) -> None:
    path = repo.stories_dir / "ST-13-01 Todo Story.md"
    original = path.read_text(encoding="utf-8")
    note = read_note(path)
    same_status = str(note.frontmatter["status"])
    same_updated = str(note.frontmatter["updated"])
    rewritten = write_note_fields(note, {"status": same_status, "updated": same_updated})
    assert rewritten == original


def test_write_note_fields_changes_only_named_fields(repo: PmVaultRepo) -> None:
    path = repo.stories_dir / "ST-13-01 Todo Story.md"
    note = read_note(path)
    rewritten = write_note_fields(note, {"status": "in_progress", "updated": "2026-09-20"})

    # body untouched (render_note is `---\n{fm}\n---\n{body}` - strip that wrapper back off)
    assert rewritten.endswith(note.body)
    new_fm_text = rewritten[len("---\n") : -(len(note.body) + len("\n---\n"))]

    before_lines = note.frontmatter_text.split("\n")
    after_lines = new_fm_text.split("\n")
    assert len(before_lines) == len(after_lines)
    changed_keys = {
        b.split(":", 1)[0] for b, a in zip(before_lines, after_lines, strict=True) if b != a
    }
    assert changed_keys == {"status", "updated"}


def test_update_story_status_writes_through_vault(repo: PmVaultRepo) -> None:
    item = repo.update_story_status("ST-13-01", "in_progress")
    assert item.status == "in_progress"
    reread = repo.read_story(repo.stories_dir / "ST-13-01 Todo Story.md")
    assert reread.status == "in_progress"
    # title/epic/priority preserved
    assert reread.epic_id == "EPIC-13"
    assert reread.priority == "P1"


def test_update_unknown_story_raises_key_error(repo: PmVaultRepo) -> None:
    with pytest.raises(KeyError):
        repo.update_story_status("ST-99-99", "in_progress")


def test_create_story_writes_new_note_from_template_shape(repo: PmVaultRepo) -> None:
    item = repo.create_story(
        story_id="ST-13-10", epic_id="EPIC-13", title="New Story", priority="P2", estimate="S"
    )
    assert item.id == "ST-13-10"
    assert item.status == "todo"
    assert item.epic_id == "EPIC-13"
    path = repo.stories_dir / "ST-13-10 New Story.md"
    assert path.is_file()


def test_create_story_refuses_existing_path(repo: PmVaultRepo) -> None:
    repo.create_story(
        story_id="ST-13-11", epic_id="EPIC-13", title="Dup", priority="P2", estimate="S"
    )
    with pytest.raises(FileExistsError):
        repo.create_story(
            story_id="ST-13-11", epic_id="EPIC-13", title="Dup", priority="P2", estimate="S"
        )


def test_projects_note_missing_field_rejected(repo: PmVaultRepo) -> None:
    repo.projects_note_path.write_text(
        "---\ntitle: Projects\nprojects:\n  - id: PRJ-X\n    name: X\n---\nbody\n", encoding="utf-8"
    )
    with pytest.raises(WorkItemValidationError) as excinfo:
        repo.read_projects()
    assert excinfo.value.field == "status"


def test_projects_note_absent_returns_empty(tmp_path: Path) -> None:
    empty_vault = tmp_path / "empty_vault"
    empty_vault.mkdir()
    repo = PmVaultRepo(
        empty_vault, epics_dir="08 - Epics", stories_dir="09 - Stories", projects_note="Projects.md"
    )
    assert repo.read_projects() == []
    assert repo.all_items() == []

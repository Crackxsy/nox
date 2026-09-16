"""nox.memory.frontmatter: parse/render, `nox: ignore`, `owner`."""

from __future__ import annotations

from pathlib import Path

from nox.memory.frontmatter import is_ignored, owner, parse_note, render_note


def test_parse_note_with_frontmatter(tmp_path: Path) -> None:
    path = tmp_path / "n.md"
    path.write_text("---\ntitle: Hello\nowner: nox\n---\n\nBody text.\n", encoding="utf-8")
    note = parse_note(path)
    assert note.frontmatter == {"title": "Hello", "owner": "nox"}
    assert note.body == "\nBody text.\n"
    assert owner(note) == "nox"


def test_parse_note_without_frontmatter(tmp_path: Path) -> None:
    path = tmp_path / "n.md"
    path.write_text("Just body, no frontmatter.\n", encoding="utf-8")
    note = parse_note(path)
    assert note.frontmatter == {}
    assert note.body == "Just body, no frontmatter.\n"
    assert owner(note) is None


def test_is_ignored() -> None:
    from nox.memory.frontmatter import VaultNote

    ignored = VaultNote(Path("x.md"), {"nox": {"ignore": True}}, "", "h", "")
    not_ignored = VaultNote(Path("x.md"), {"nox": {"ignore": False}}, "", "h", "")
    no_nox_key = VaultNote(Path("x.md"), {}, "", "h", "")
    assert is_ignored(ignored) is True
    assert is_ignored(not_ignored) is False
    assert is_ignored(no_nox_key) is False


def test_render_note_roundtrip() -> None:
    text = render_note({"title": "Hi", "nox": {"written": True}}, "\nbody\n")
    assert text.startswith("---\n")
    assert "title: Hi" in text
    assert text.endswith("\nbody\n")


def test_render_note_no_frontmatter_is_bare_body() -> None:
    assert render_note({}, "just body") == "just body"

"""`personality.md`: created from the neutral default, and a user edit always wins."""

from __future__ import annotations

import re
from pathlib import Path

from nox.ai import prompting
from nox.ai.prompting import DEFAULT_PERSONALITY_BLOCK, RULES_BLOCK, build_system_prompt
from nox.settings.personality import PersonalityFile, render_default


def test_the_repo_default_is_neutral_and_carries_no_personal_data() -> None:
    text = str(DEFAULT_PERSONALITY_BLOCK).lower()
    # The repository ships a character, not a person: no names, no nicknames, no biography.
    assert "{user_name}" in str(DEFAULT_PERSONALITY_BLOCK)
    assert "{assistant_name}" in str(DEFAULT_PERSONALITY_BLOCK)
    for token in ("niklas", "crackxsy"):
        assert token not in text
    # Numbered dimensions stay, so an owner editing the file has the same structure to fill in.
    numbers = [int(m) for m in re.findall(r"^(\d+)\. ", str(DEFAULT_PERSONALITY_BLOCK), re.M)]
    assert numbers == list(range(1, len(numbers) + 1))
    assert "Not yet specified" in str(DEFAULT_PERSONALITY_BLOCK)


def test_render_default_fills_the_placeholders() -> None:
    text = render_default(assistant_name="Aria", user_name="Sam")
    assert text.startswith("You are Aria.")
    assert "companion to Sam" in text
    assert "{user_name}" not in text and "{assistant_name}" not in text


def test_the_file_is_created_from_the_default_on_first_read(tmp_path: Path) -> None:
    file = PersonalityFile(tmp_path / "data" / "personality.md", user_name="Sam")

    assert not file.path.exists()
    text = file.read()

    assert file.path.is_file()
    assert text == render_default(user_name="Sam")


def test_an_existing_file_is_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "personality.md"
    path.write_text("You are Nox. Be brief.", encoding="utf-8")
    file = PersonalityFile(path)

    file.ensure()

    assert path.read_text(encoding="utf-8") == "You are Nox. Be brief."


def test_a_user_edit_wins_on_the_next_read(tmp_path: Path) -> None:
    path = tmp_path / "personality.md"
    file = PersonalityFile(path)
    first = file.read()

    path.write_text("You are Nox. Edited by hand.", encoding="utf-8")
    second = file.read()

    assert first != second
    assert second == "You are Nox. Edited by hand."


def test_write_replaces_the_text_and_an_empty_write_restores_the_default(tmp_path: Path) -> None:
    file = PersonalityFile(tmp_path / "personality.md")

    file.write("You are Nox. Dashboard edit.")
    assert file.read() == "You are Nox. Dashboard edit."

    file.write("   ")
    assert file.read() == file.default_text()


def test_the_prompt_builder_uses_the_configured_file(tmp_path: Path) -> None:
    file = PersonalityFile(tmp_path / "personality.md")
    file.write("You are Nox. From the file.")
    prompting.set_personality_source(file.read)
    try:
        prompt = build_system_prompt(DEFAULT_PERSONALITY_BLOCK, {})
        assert prompt == "You are Nox. From the file.\n\n" + RULES_BLOCK
    finally:
        prompting.set_personality_source(None)

    # Without a source installed the neutral built-in default applies again.
    assert build_system_prompt(DEFAULT_PERSONALITY_BLOCK, {}).startswith("You are {assistant_name}")


def test_an_explicit_block_is_never_replaced(tmp_path: Path) -> None:
    file = PersonalityFile(tmp_path / "personality.md")
    file.write("You are Nox. From the file.")
    prompting.set_personality_source(file.read)
    try:
        assert build_system_prompt("A literal block", {}) == "A literal block\n\n" + RULES_BLOCK
    finally:
        prompting.set_personality_source(None)

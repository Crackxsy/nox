"""`personality.md`: created from the neutral default, and a user edit always wins."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from nox.ai import prompting
from nox.ai.prompting import (
    ANSWER_SHAPE_BLOCK,
    DEFAULT_PERSONALITY_BLOCK,
    RULES_BLOCK,
    build_system_prompt,
)
from nox.settings.personality import PersonalityFile, render_default

#: Parts of a git identity that are infrastructure rather than a person. Without this, a forge
#: e-mail contributes words like "github" and the check would fail on ordinary prose.
_NOT_A_NAME = frozenset(
    {
        "users",
        "noreply",
        "github",
        "gitlab",
        "gmail",
        "outlook",
        "hotmail",
        "mail",
        "email",
        "localhost",
        "example",
    }
)

#: Everything `build_system_prompt` appends after the personality when there are no facts.
_STABLE_TAIL = "\n\n" + RULES_BLOCK + "\n\n" + ANSWER_SHAPE_BLOCK


def _local_identity() -> list[str]:
    """Names git knows about on this machine, as lower-case words.

    The check below needs something concrete to look for, and naming the maintainer in the file
    would put in the repository exactly what the test exists to keep out. Asking git instead means
    the test protects whoever is working right now - on a contributor's laptop, their name; in CI,
    the runner's - and the repository stays free of anyone's.
    """
    words: list[str] = []
    for key in ("user.name", "user.email"):
        try:
            value = subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["git", "config", "--get", key],  # noqa: S607 - git is on PATH in dev and CI
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git, nothing to check
            continue
        words += [
            word
            for word in re.split(r"[^A-Za-z0-9]+", value.lower())
            if len(word) > 3 and word not in _NOT_A_NAME
        ]
    return words


def test_the_repo_default_is_neutral_and_carries_no_personal_data() -> None:
    text = str(DEFAULT_PERSONALITY_BLOCK).lower()
    # The repository ships a character, not a person: no names, no nicknames, no biography.
    assert "{user_name}" in str(DEFAULT_PERSONALITY_BLOCK)
    assert "{assistant_name}" in str(DEFAULT_PERSONALITY_BLOCK)
    for word in _local_identity():
        assert word not in text, f"the shipped personality names {word!r}"
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
        assert prompt == "You are Nox. From the file." + _STABLE_TAIL
    finally:
        prompting.set_personality_source(None)

    # Without a source installed the neutral built-in default applies again.
    assert build_system_prompt(DEFAULT_PERSONALITY_BLOCK, {}).startswith("You are {assistant_name}")


def test_an_explicit_block_is_never_replaced(tmp_path: Path) -> None:
    file = PersonalityFile(tmp_path / "personality.md")
    file.write("You are Nox. From the file.")
    prompting.set_personality_source(file.read)
    try:
        assert build_system_prompt("A literal block", {}) == "A literal block" + _STABLE_TAIL
    finally:
        prompting.set_personality_source(None)

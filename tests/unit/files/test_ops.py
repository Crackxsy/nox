"""What the file operations do, and the three things they refuse to do quietly.

A model tidying a folder will sometimes be wrong about which file. So: a write never replaces
something without being told to, a write never invents folders, and a read refuses anything that is
not text. Each of those is a loss that would otherwise happen by accident.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nox.files import ops
from nox.files.recycle import RecycleError
from nox.files.roots import OutsideRootsError


@pytest.fixture
def root(tmp_path: Path) -> Path:
    inside = tmp_path / "files"
    inside.mkdir()
    # Bytes, not `write_text`: that translates the newline on Windows, and a test about exact
    # content has to control the content exactly. The tools write with newline="" for the
    # same reason - what the model wrote is what lands in the file.
    (inside / "notes.txt").write_bytes(b"erste Zeile\n")
    (inside / "bild.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00binary")
    (inside / "unterordner").mkdir()
    return inside


def roots(root: Path) -> list[str]:
    return [str(root)]


# ---- looking ----------------------------------------------------------------------------------


async def test_a_listing_puts_folders_first(root: Path) -> None:
    answer = await ops.list_dir(str(root), roots(root), limit=10)

    assert answer["ok"]
    names = [entry["name"] for entry in answer["entries"]]
    assert names == ["unterordner", "bild.png", "notes.txt"]
    assert answer["entries"][0]["kind"] == "folder"
    assert answer["entries"][2]["bytes"] == len("erste Zeile\n")


async def test_a_long_listing_says_it_was_cut(root: Path) -> None:
    """A model told 200 of 40000 entries must be able to tell it is looking at a part."""
    for index in range(5):
        (root / f"f{index}.txt").write_text("x", encoding="utf-8")

    answer = await ops.list_dir(str(root), roots(root), limit=3)

    assert answer["truncated"] is True
    assert answer["total"] == 8
    assert len(answer["entries"]) == 3


async def test_listing_a_file_says_it_is_a_file(root: Path) -> None:
    answer = await ops.list_dir(str(root / "notes.txt"), roots(root), limit=10)

    assert answer["ok"] is False and "not a folder" in answer["error"]


async def test_reading_returns_the_text(root: Path) -> None:
    answer = await ops.read_text(str(root / "notes.txt"), roots(root), max_bytes=1000)

    assert answer["ok"] and answer["text"] == "erste Zeile\n"
    assert answer["encoding"] == "utf-8" and answer["truncated"] is False


async def test_reading_a_picture_is_refused(root: Path) -> None:
    """A JPEG in a prompt is nonsense that costs money and teaches the model nothing."""
    answer = await ops.read_text(str(root / "bild.png"), roots(root), max_bytes=1000)

    assert answer["ok"] is False and "not a text file" in answer["error"]


async def test_a_long_read_is_capped_and_says_so(root: Path) -> None:
    (root / "lang.txt").write_text("a" * 500, encoding="utf-8")

    answer = await ops.read_text(str(root / "lang.txt"), roots(root), max_bytes=100)

    assert len(answer["text"]) == 100
    assert answer["truncated"] is True and answer["bytes"] == 500


async def test_a_file_the_windows_editor_wrote_is_still_readable(root: Path) -> None:
    (root / "alt.txt").write_bytes("Grüße".encode("cp1252"))

    answer = await ops.read_text(str(root / "alt.txt"), roots(root), max_bytes=1000)

    assert answer["ok"] and answer["text"] == "Grüße" and answer["encoding"] == "cp1252"


# ---- changing ---------------------------------------------------------------------------------


async def test_writing_a_new_file_works(root: Path) -> None:
    answer = await ops.write_text(str(root / "neu.txt"), "inhalt", roots(root), max_bytes=1000)

    assert answer["ok"]
    assert (root / "neu.txt").read_text(encoding="utf-8") == "inhalt"


async def test_an_existing_file_is_not_replaced_by_accident(root: Path) -> None:
    answer = await ops.write_text(str(root / "notes.txt"), "weg", roots(root), max_bytes=1000)

    assert answer["ok"] is False and "already exists" in answer["error"]
    assert (root / "notes.txt").read_text(encoding="utf-8") == "erste Zeile\n"


async def test_an_existing_file_is_replaced_when_that_is_the_request(root: Path) -> None:
    answer = await ops.write_text(
        str(root / "notes.txt"), "neu", roots(root), max_bytes=1000, overwrite=True
    )

    assert answer["ok"]
    assert (root / "notes.txt").read_text(encoding="utf-8") == "neu"


async def test_appending_keeps_what_was_there(root: Path) -> None:
    answer = await ops.write_text(
        str(root / "notes.txt"), "zweite Zeile\n", roots(root), max_bytes=1000, append=True
    )

    assert answer["ok"] and answer["appended"] is True
    assert (root / "notes.txt").read_text(encoding="utf-8") == "erste Zeile\nzweite Zeile\n"


async def test_a_missing_folder_is_not_invented(root: Path) -> None:
    """A typo in a folder name would otherwise build a tree in a place nobody looks."""
    answer = await ops.write_text(
        str(root / "tippfehler" / "x.txt"), "inhalt", roots(root), max_bytes=1000
    )

    assert answer["ok"] is False and "does not exist" in answer["error"]
    assert not (root / "tippfehler").exists()


async def test_a_write_over_the_size_limit_is_refused(root: Path) -> None:
    answer = await ops.write_text(str(root / "gross.txt"), "x" * 50, roots(root), max_bytes=10)

    assert answer["ok"] is False and "the limit is 10" in answer["error"]
    assert not (root / "gross.txt").exists()


async def test_moving_renames(root: Path) -> None:
    answer = await ops.move(str(root / "notes.txt"), str(root / "notizen.txt"), roots(root))

    assert answer["ok"]
    assert (root / "notizen.txt").exists() and not (root / "notes.txt").exists()


async def test_moving_into_a_folder_keeps_the_name(root: Path) -> None:
    answer = await ops.move(str(root / "notes.txt"), str(root / "unterordner"), roots(root))

    assert answer["ok"]
    assert (root / "unterordner" / "notes.txt").exists()


async def test_moving_onto_something_that_exists_is_refused(root: Path) -> None:
    answer = await ops.move(str(root / "notes.txt"), str(root / "bild.png"), roots(root))

    assert answer["ok"] is False and "already exists" in answer["error"]
    assert (root / "notes.txt").exists()


async def test_moving_out_of_the_roots_is_refused(root: Path) -> None:
    with pytest.raises(OutsideRootsError):
        await ops.move(str(root / "notes.txt"), str(root.parent / "weg.txt"), roots(root))

    assert (root / "notes.txt").exists()


# ---- deleting ---------------------------------------------------------------------------------


async def test_deleting_goes_through_the_recycle_bin(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patched on purpose: a real run would leave a file in the user's bin every time.

    That the shell call itself works was verified by hand on Windows. What is asserted here is that
    `ops.delete` reaches it with the *resolved* path and never falls back to an unlink.
    """
    seen: list[Path] = []
    monkeypatch.setattr("nox.files.ops.to_recycle_bin", lambda path: seen.append(path))

    answer = await ops.delete(str(root / ".." / "files" / "notes.txt"), roots(root))

    assert answer["ok"] and answer["recoverable"] is True
    assert seen == [root / "notes.txt"]


async def test_a_refused_delete_leaves_the_file_alone(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(_path: Path) -> None:
        raise RecycleError("the shell said no")

    monkeypatch.setattr("nox.files.ops.to_recycle_bin", refuse)

    answer = await ops.delete(str(root / "notes.txt"), roots(root))

    assert answer["ok"] is False and "the shell said no" in answer["error"]
    assert (root / "notes.txt").exists()


async def test_deleting_outside_the_roots_never_reaches_the_shell(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[Any] = []
    monkeypatch.setattr("nox.files.ops.to_recycle_bin", lambda path: called.append(path))

    with pytest.raises(OutsideRootsError):
        await ops.delete(str(root.parent), roots(root))

    assert called == []

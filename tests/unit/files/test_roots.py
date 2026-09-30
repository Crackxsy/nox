"""The boundary. Everything the file tools do goes through this, so these are the tests that matter.

The interesting one is `test_a_junction_out_of_a_root_is_refused`. Checking the text of a path and
resolving it afterwards is the classic way to let `roots/../../Windows` through, and on Windows a
junction is the same hole without the giveaway dots - a folder inside the root that simply is
somewhere else. Both are covered, and the junction one creates a real junction rather than a mock,
because a mock would pass whatever the implementation happened to do.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nox.files.roots import OutsideRootsError, resolve_inside, resolved_roots


@pytest.fixture
def root(tmp_path: Path) -> Path:
    inside = tmp_path / "allowed"
    inside.mkdir()
    (inside / "notes.txt").write_text("hallo", encoding="utf-8")
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "passwords.txt").write_text("nope", encoding="utf-8")
    return inside


def test_a_path_inside_a_root_resolves(root: Path) -> None:
    assert resolve_inside(str(root / "notes.txt"), [str(root)], must_exist=True) == (
        root / "notes.txt"
    )


def test_the_root_itself_is_inside_itself(root: Path) -> None:
    assert resolve_inside(str(root), [str(root)]) == root


def test_no_configured_root_means_nothing_is_reachable(root: Path) -> None:
    """The inversion to get wrong: an empty list must mean nowhere, never everywhere."""
    with pytest.raises(OutsideRootsError, match="files.roots"):
        resolve_inside(str(root / "notes.txt"), [])


def test_a_path_outside_every_root_is_refused(root: Path) -> None:
    with pytest.raises(OutsideRootsError, match="outside the folders"):
        resolve_inside(str(root.parent / "secret" / "passwords.txt"), [str(root)])


def test_dot_dot_cannot_climb_out(root: Path) -> None:
    escape = str(root / ".." / "secret" / "passwords.txt")

    with pytest.raises(OutsideRootsError, match="outside the folders"):
        resolve_inside(escape, [str(root)])


def test_a_junction_out_of_a_root_is_refused(root: Path) -> None:
    """A folder inside the root that is really somewhere else. `..` with no dots to notice."""
    link = root / "shortcut"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(root.parent / "secret")],
        capture_output=True,
        check=False,
    )
    if created.returncode != 0:  # pragma: no cover - a machine that cannot make junctions
        # Bytes, not text: the console speaks the machine's language in its own code page, and
        # decoding that as cp1252 throws before the skip message is ever read.
        why = created.stderr.decode("utf-8", errors="replace").strip()
        pytest.skip(f"cannot create a junction here: {why}")

    with pytest.raises(OutsideRootsError, match="outside the folders"):
        resolve_inside(str(link / "passwords.txt"), [str(root)])


def test_a_file_that_is_not_there_is_refused_when_it_has_to_be(root: Path) -> None:
    with pytest.raises(OutsideRootsError, match="does not exist"):
        resolve_inside(str(root / "ghost.txt"), [str(root)], must_exist=True)


def test_a_file_that_is_not_there_is_fine_for_a_write(root: Path) -> None:
    assert resolve_inside(str(root / "new.txt"), [str(root)]) == root / "new.txt"


def test_an_empty_path_is_refused(root: Path) -> None:
    with pytest.raises(OutsideRootsError, match="no path"):
        resolve_inside("   ", [str(root)])


def test_a_root_that_is_not_there_is_not_a_root(tmp_path: Path) -> None:
    """An unplugged drive is not an error, but a path "inside" it cannot be checked either."""
    assert resolved_roots([str(tmp_path / "nowhere")]) == []


def test_a_root_that_is_a_file_is_not_a_root(root: Path) -> None:
    assert resolved_roots([str(root / "notes.txt")]) == []


def test_several_roots_are_all_honoured(tmp_path: Path) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    for folder in (first, second):
        folder.mkdir()

    roots = [str(first), str(second)]

    assert resolve_inside(str(second / "x.txt"), roots) == second / "x.txt"

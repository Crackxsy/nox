"""The version is declared in three files, so the three have to agree.

They did not. `pyproject.toml` and `nox.__version__` said `0.1.0.dev0` while the installer said
`0.2.0` and the changelog's newest release was `0.2.0` - three declarations, two values, none of
them true. Nothing broke, which is exactly why it drifted: a wrong version number is invisible until
somebody is trying to work out which build they are running.

Keeping one of them as the source and deriving the others would be better and is not possible here:
the Inno Setup script is read by a compiler that knows nothing about Python, and `pyproject.toml`
cannot import anything. So they are checked instead.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import nox

REPO = Path(__file__).resolve().parents[2]


def pyproject_version() -> str:
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def installer_version() -> str:
    text = (REPO / "installer" / "nox.iss").read_text(encoding="utf-8")
    found = re.search(r'#define\s+MyAppVersion\s+"([^"]+)"', text)
    assert found is not None, "the installer no longer declares MyAppVersion"
    return found.group(1)


def changelog_version() -> str:
    """The newest released section. `[Unreleased]` is deliberately skipped."""
    text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    for line in text.splitlines():
        found = re.match(r"^## \[(\d+\.\d+\.\d+)\]", line)
        if found:
            return found.group(1)
    raise AssertionError("the changelog has no released version")


def test_every_declaration_agrees() -> None:
    assert nox.__version__ == pyproject_version() == installer_version()


def test_the_changelog_has_a_section_for_it() -> None:
    """A release nobody wrote down is a release nobody can read the notes for."""
    assert changelog_version() == nox.__version__


def test_the_version_is_a_release_number() -> None:
    """`0.1.0.dev0` sat in two files for two releases. A dev marker is not a released version."""
    assert re.fullmatch(r"\d+\.\d+\.\d+", nox.__version__), nox.__version__

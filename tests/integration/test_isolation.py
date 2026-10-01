"""The integration tests' own guarantee (`conftest.py`): no core they boot sees real replays."""

from __future__ import annotations

from pathlib import Path

from nox.rl.paths import default_replay_folder


def test_the_replay_folder_is_inside_the_tests_own_documents(_empty_documents_folder: Path) -> None:
    folder = default_replay_folder()
    assert folder.is_relative_to(_empty_documents_folder)
    assert not folder.exists()  # empty: nothing for the backfill to parse

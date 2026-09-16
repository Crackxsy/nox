"""ST-12-02 AC: resolve the real path via the Windows special-folder API, not a hardcoded drive
letter."""

from __future__ import annotations

from nox.rl.paths import default_replay_folder, documents_dir


def test_default_replay_folder_ends_with_the_expected_subpath() -> None:
    folder = default_replay_folder()
    parts = folder.parts[-4:]
    assert parts == ("My Games", "Rocket League", "TAGame", "DemosEpic")


def test_default_replay_folder_is_under_documents_dir() -> None:
    assert str(default_replay_folder()).startswith(str(documents_dir()))

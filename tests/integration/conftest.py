"""Isolation every integration test gets: a real `NoxCore` never reaches into this machine's own
Documents folder.

The Rocket League extension backfills every replay under
`Documents/My Games/Rocket League/TAGame/DemosEpic`. On a developer's machine that is thousands of
real replays, parsed in the background of every test that boots a core - slow, a source of
timeouts under load, and the developer's own data. CI has none, so it never noticed. Each test
gets an empty Documents folder of its own instead; a test that wants replays puts them there or
passes its own folder, as `test_rl_plugin.py` does.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _empty_documents_folder(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    documents = tmp_path_factory.mktemp("Documents")
    monkeypatch.setattr("nox.rl.paths.documents_dir", lambda: documents)
    return documents

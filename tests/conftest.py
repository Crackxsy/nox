"""Fixtures for every test suite.

Each test runs against its own Nox folder. Without this, a test that resolves the user layer, the
runtime directory or the secret store would read - or write - the real ones of whoever runs the
suite: their `user.yaml`, and on a Linux machine without a keyring their `secrets.json`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_nox_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    home = tmp_path_factory.mktemp("nox-home")
    monkeypatch.setenv("NOX_HOME", str(home))
    monkeypatch.delenv("NOX_USER_CONFIG", raising=False)
    yield home

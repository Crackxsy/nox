"""Fixtures for every test suite.

Each test runs against its own Nox folder. Without this, a test that resolves the user layer, the
runtime directory or the secret store would read - or write - the real ones of whoever runs the
suite: their `user.yaml`, and on a Linux machine without a keyring their `secrets.json`.
"""

from __future__ import annotations

import sys
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


@pytest.fixture(autouse=True, scope="session")
def _sandbox_grants_outlive_no_test(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Windows: every runtime ACE a test's AppContainer grants is recorded in ONE manifest for the
    whole session and removed when it ends.

    With the per-test Nox folder above, each test had its own manifest: an ACE granted on the real
    interpreter folder was recorded in a folder that was deleted right after, so nothing ever
    removed it - and a container-SID ACE on the interpreter makes low-integrity Python fail to
    start, which made the later process tests fail or wait on a child that never came up.
    """
    if sys.platform != "win32":
        yield
        return
    from nox.sandbox import windows

    manifest = tmp_path_factory.mktemp("sandbox-grants") / "acl-grants.json"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(windows, "default_grants_manifest", lambda: manifest)
        try:
            yield
        finally:
            windows.RuntimeGrants(manifest).reset()

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


#: How long before pytest-timeout fires the test that is still running is named on stderr.
_NAME_BEFORE_TIMEOUT_S = 2.0


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> Iterator[None]:
    """Name a test that is about to hit its timeout.

    With `-q` and `timeout_method = "thread"`, pytest-timeout dumps every thread's stack and ends
    the process - the progress dots never say which test it was (a Windows run once stopped at
    71 % with nothing but stacks). Shortly before the timeout this prints the test's id, so the
    log names it. A test that finishes in time prints nothing.
    """
    import threading

    marker = item.get_closest_marker("timeout")
    raw = marker.args[0] if marker is not None and marker.args else item.config.getini("timeout")
    try:
        limit = float(raw or 0)
    except (TypeError, ValueError):
        limit = 0.0
    timer: threading.Timer | None = None
    if limit > _NAME_BEFORE_TIMEOUT_S:

        def name_it() -> None:
            sys.stderr.write(
                f"\n+++ still running after {limit - _NAME_BEFORE_TIMEOUT_S:g} s: {item.nodeid}\n"
            )
            sys.stderr.flush()

        timer = threading.Timer(limit - _NAME_BEFORE_TIMEOUT_S, name_it)
        timer.daemon = True
        timer.start()
    try:
        yield
    finally:
        if timer is not None:
            timer.cancel()

"""The benchmark world never touches the user's own Nox: no real keyring, no real NOX_HOME."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kann_alles.env import World
from nox.security.secrets import open_secret_store


def test_the_world_uses_its_own_home_and_a_file_secret_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOX_HOME", str(tmp_path / "users-real-nox-home"))
    world = World(tmp_path / "world")

    world.isolate()
    try:
        config = world._config()  # noqa: SLF001
        home = Path(os.environ["NOX_HOME"])
    finally:
        world._restore_env()  # noqa: SLF001

    assert config.security.secret_store == "file"
    assert home.is_relative_to(tmp_path / "world")
    assert os.environ["NOX_HOME"] == str(tmp_path / "users-real-nox-home"), "restored afterwards"


def test_the_world_secret_store_never_reaches_the_keyring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import keyring

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the benchmark touched the real keyring")

    for name in ("get_password", "set_password", "delete_password", "get_keyring"):
        monkeypatch.setattr(keyring, name, forbidden)
    world = World(tmp_path / "world")
    world.isolate()
    try:
        config = world._config()  # noqa: SLF001
        store = open_secret_store(config.security.secret_store)
        store.set("nox/home/token", "eval-only")
        value = store.get("nox/home/token")
    finally:
        world._restore_env()  # noqa: SLF001

    assert store.backend == "file" and value == "eval-only"

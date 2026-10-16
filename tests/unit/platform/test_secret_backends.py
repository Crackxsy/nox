"""Which secret store an installation uses, and what happens when it goes away."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

import nox.security.secrets as secrets_module
from nox.core.boot.health import core_health_checks
from nox.core.events import HealthStatus
from nox.security.file_secrets import FileSecretStore
from nox.security.secret_names import SecretStoreError, SecretStoreUnavailableError
from nox.security.secrets import (
    BACKEND_MARKER,
    InMemorySecretStore,
    KeyringSecretStore,
    PinManager,
    UnavailableSecretStore,
    open_secret_store,
    resolve_secret_backend,
)


def _keyring(monkeypatch: pytest.MonkeyPatch, usable: bool) -> None:
    monkeypatch.setattr(secrets_module, "keyring_usable", lambda module=None: usable)


def test_the_first_choice_is_remembered(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _keyring(monkeypatch, usable=False)
    assert resolve_secret_backend("auto", home=tmp_path) == "file"
    assert (tmp_path / BACKEND_MARKER).read_text(encoding="utf-8").strip() == "file"
    # A keyring appearing later (a desktop installed on the Pi) does not move the secrets.
    _keyring(monkeypatch, usable=True)
    assert resolve_secret_backend("auto", home=tmp_path) == "file"


def test_a_keyring_that_goes_away_is_not_replaced_by_an_empty_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _keyring(monkeypatch, usable=True)
    assert resolve_secret_backend("auto", home=tmp_path) == "keyring"
    _keyring(monkeypatch, usable=False)
    store = open_secret_store("auto", home=tmp_path)
    assert isinstance(store, UnavailableSecretStore)
    assert not (tmp_path / "secrets.json").exists()
    with pytest.raises(SecretStoreUnavailableError):
        store.get("nox/security/pin")


def test_an_unreachable_store_keeps_the_pin_gate_closed(tmp_path: Path) -> None:
    pin = PinManager(UnavailableSecretStore("keyring gone"))
    assert pin.is_set() is True  # fail-closed: a gate that needs the PIN keeps needing it
    status = pin.verify_pin("1234")
    assert status.ok is False and "secret store unavailable" in status.reason


def test_a_forced_backend_wins_and_is_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _keyring(monkeypatch, usable=True)
    assert isinstance(open_secret_store("file", home=tmp_path), FileSecretStore)
    assert (tmp_path / BACKEND_MARKER).read_text(encoding="utf-8").strip() == "file"
    assert isinstance(open_secret_store("keyring", home=tmp_path), KeyringSecretStore)
    assert (tmp_path / BACKEND_MARKER).read_text(encoding="utf-8").strip() == "keyring"


def test_two_stores_on_one_file_do_not_lose_updates(tmp_path: Path) -> None:
    first, second = FileSecretStore(tmp_path), FileSecretStore(tmp_path)

    def writer(store: FileSecretStore, prefix: str) -> None:
        for i in range(25):
            store.set(f"nox/test/{prefix}{i}", str(i))

    threads = [
        threading.Thread(target=writer, args=(first, "a")),
        threading.Thread(target=writer, args=(second, "b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(FileSecretStore(tmp_path).names()) == 50
    assert not list(tmp_path.glob("*.tmp"))


def test_a_corrupt_file_is_an_error_not_a_crash(tmp_path: Path) -> None:
    (tmp_path / "secrets.json").write_text("{not json", encoding="utf-8")
    if sys.platform != "win32":
        os.chmod(tmp_path / "secrets.json", 0o600)
    with pytest.raises(SecretStoreError, match="cannot be read"):
        FileSecretStore(tmp_path).get("nox/home/access_token")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership and modes")
def test_a_loose_or_linked_file_is_refused(tmp_path: Path) -> None:
    store = FileSecretStore(tmp_path)
    store.set("nox/home/access_token", "t0k3n")
    os.chmod(tmp_path / "secrets.json", 0o644)
    with pytest.raises(SecretStoreError, match="readable by others"):
        store.get("nox/home/access_token")
    os.chmod(tmp_path / "secrets.json", 0o600)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    target = elsewhere / "secrets.json"
    os.replace(tmp_path / "secrets.json", target)
    (tmp_path / "secrets.json").symlink_to(target)
    with pytest.raises(SecretStoreError, match="not a regular file"):
        store.get("nox/home/access_token")


async def test_the_health_line_names_the_store() -> None:
    for store, status, words in [
        (KeyringSecretStore(backend=object()), HealthStatus.AVAILABLE, "OS keyring"),
        (FileSecretStore(Path("unused")), HealthStatus.LIMITED, "owner-only file"),
        (UnavailableSecretStore("gone"), HealthStatus.UNAVAILABLE, "unreachable"),
        (InMemorySecretStore(), HealthStatus.AVAILABLE, "memory"),
    ]:
        checks = core_health_checks(
            database=lambda: None,
            vault_dir=lambda: Path("."),
            workers=None,  # type: ignore[arg-type]
            voice_enabled=False,
            tokens=lambda: None,
            providers=lambda: [],
            secrets=lambda s=store: s,
        )
        check = next(c for c in checks if c.name == "secrets")
        got_status, detail = await check.probe()
        assert got_status is status and words in detail


def test_a_plugin_worker_gets_the_folder_but_no_library_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nox.plugins.manager import _worker_environment

    monkeypatch.setenv("NOX_HOME", "/var/lib/nox")
    monkeypatch.setenv("HOME", "/var/lib/nox")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/evil")
    monkeypatch.setenv("SOME_API_KEY", "secret")
    env = _worker_environment()
    assert env["NOX_HOME"] == "/var/lib/nox" and env["HOME"] == "/var/lib/nox"
    assert "LD_LIBRARY_PATH" not in env and "SOME_API_KEY" not in env


def test_a_windows_style_path_lands_in_the_nox_folder_elsewhere(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from nox.core.config.types import expand_path

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("NOX_HOME", str(tmp_path / "srv" / "Nox"))
    assert expand_path("${APPDATA}/Nox") == tmp_path / "srv" / "Nox"
    assert expand_path("${NOX_HOME}/runtime") == tmp_path / "srv" / "Nox" / "runtime"


def test_a_relative_nox_home_is_made_absolute(monkeypatch: pytest.MonkeyPatch) -> None:
    from nox.paths import nox_home

    monkeypatch.setenv("NOX_HOME", "relative-folder")
    assert nox_home().is_absolute()

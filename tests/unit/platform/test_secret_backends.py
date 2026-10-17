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
    monkeypatch.setattr(sys, "platform", "linux")
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
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    # The Pi service's folder is lowercase `/var/lib/nox`: `${APPDATA}/Nox` must still land there.
    monkeypatch.setenv("NOX_HOME", str(tmp_path / "var" / "lib" / "nox"))
    home = tmp_path / "var" / "lib" / "nox"
    assert expand_path("${APPDATA}/Nox") == home
    assert expand_path("%APPDATA%/Nox/data/clips") == home / "data" / "clips"
    assert expand_path("$LOCALAPPDATA/Nox/runtime") == home / "runtime"
    assert expand_path("${NOX_HOME}/runtime") == home / "runtime"


def test_a_relative_nox_home_is_made_absolute(monkeypatch: pytest.MonkeyPatch) -> None:
    from nox.paths import nox_home

    monkeypatch.setenv("NOX_HOME", "relative-folder")
    assert nox_home().is_absolute()


def test_looking_never_decides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _keyring(monkeypatch, usable=False)
    monkeypatch.setattr(sys, "platform", "linux")
    assert resolve_secret_backend("auto", home=tmp_path, record=False) == "file"
    assert not (tmp_path / BACKEND_MARKER).exists()


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_a_desktop_os_never_chooses_the_file_by_itself(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str
) -> None:
    # An upgrade whose first start meets a keyring that hiccups must not be pinned to an empty file.
    _keyring(monkeypatch, usable=False)
    monkeypatch.setattr(sys, "platform", platform)
    assert resolve_secret_backend("auto", home=tmp_path) == "unavailable"
    assert not (tmp_path / BACKEND_MARKER).exists()
    _keyring(monkeypatch, usable=True)
    assert resolve_secret_backend("auto", home=tmp_path) == "keyring"


def test_an_existing_secrets_file_is_kept_on_linux(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _keyring(monkeypatch, usable=True)
    FileSecretStore(tmp_path).set("nox/home/access_token", "t0k3n")
    monkeypatch.setattr(sys, "platform", "linux")
    assert resolve_secret_backend("auto", home=tmp_path) == "file"


def test_keyring_backend_errors_keep_the_gate_closed() -> None:
    class Broken:
        def get_password(self, *_a: object) -> str:
            raise RuntimeError("dbus went away")

        def set_password(self, *_a: object) -> None:
            raise RuntimeError("locked")

        def delete_password(self, *_a: object) -> None:
            raise RuntimeError("locked")

    store = KeyringSecretStore(backend=Broken())
    with pytest.raises(SecretStoreUnavailableError, match="RuntimeError"):
        store.get("nox/security/pin")
    with pytest.raises(SecretStoreUnavailableError):
        store.set("nox/security/pin", "x")
    with pytest.raises(SecretStoreUnavailableError):
        store.delete("nox/security/pin")
    assert PinManager(store).is_set() is True


def test_reading_an_empty_store_creates_nothing(tmp_path: Path) -> None:
    home = tmp_path / "not-yet"
    store = FileSecretStore(home)
    assert store.get("nox/home/access_token") is None and store.names() == []
    assert not home.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX directory modes")
def test_a_fresh_folder_is_usable_and_private(tmp_path: Path) -> None:
    home = tmp_path / "fresh" / "Nox"
    store = FileSecretStore(home)
    store.set("nox/home/access_token", "t0k3n")
    assert store.get("nox/home/access_token") == "t0k3n"
    assert home.stat().st_mode & 0o777 == 0o700
    os.chmod(home, 0o755)
    store.set("nox/home/access_token", "again")
    assert home.stat().st_mode & 0o777 == 0o700


def test_nox_secrets_reports_a_broken_store_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner

    from nox.cli import app

    monkeypatch.setattr(
        secrets_module, "open_configured_secret_store", lambda: UnavailableSecretStore("gone")
    )
    result = CliRunner().invoke(app, ["secrets", "check", "nox/home/access_token"])
    assert result.exit_code == 1 and "gone" in result.output
    assert "Traceback" not in result.output


def test_nox_where_names_the_store_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, _isolated_nox_home: Path
) -> None:
    from typer.testing import CliRunner

    from nox.cli import app

    _keyring(monkeypatch, usable=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("APPDATA", raising=False)
    result = CliRunner().invoke(app, ["where"])
    assert result.exit_code == 0, result.output
    assert "owner-only file" in result.output and str(_isolated_nox_home) in result.output
    assert not (_isolated_nox_home / BACKEND_MARKER).exists()


def test_the_user_layer_is_found_in_the_nox_folder(_isolated_nox_home: Path) -> None:
    from nox.paths import resolve_config_paths

    (_isolated_nox_home / "user.yaml").write_text("identity: {}\n", encoding="utf-8")
    assert resolve_config_paths()[1] == _isolated_nox_home / "user.yaml"


def test_a_windows_tail_gets_forward_slashes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from nox.core.config.types import expand_path

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("NOX_HOME", str(tmp_path / "nox"))
    windows_style = "%APPDATA%" + chr(92) + "Nox" + chr(92) + "data" + chr(92) + "clips"
    assert expand_path(windows_style) == tmp_path / "nox" / "data" / "clips"


def test_the_secret_store_cannot_be_switched_from_the_dashboard() -> None:
    # Switching stores makes the PIN look unset; it must take a file edit on the machine itself.
    from nox.settings.schema import EDITABLE_PATHS

    assert "security.secret_store" not in EDITABLE_PATHS


def test_the_cli_and_wizard_use_the_configured_store(
    monkeypatch: pytest.MonkeyPatch, _isolated_nox_home: Path
) -> None:
    (_isolated_nox_home / "user.yaml").write_text(
        "security:\n  secret_store: file\n", encoding="utf-8"
    )
    store = secrets_module.open_configured_secret_store()
    assert isinstance(store, FileSecretStore) and store.path.parent == _isolated_nox_home


async def test_nox_doctor_names_the_secret_store(monkeypatch: pytest.MonkeyPatch) -> None:
    from nox.core.config import NoxConfig
    from nox.entrypoints import _secrets_line

    monkeypatch.setattr(secrets_module, "resolve_secret_backend", lambda *a, **kw: "unavailable")
    assert _secrets_line(NoxConfig()).startswith("[FAIL] secrets:")
    monkeypatch.setattr(secrets_module, "resolve_secret_backend", lambda *a, **kw: "file")
    assert "owner-only file" in _secrets_line(NoxConfig())

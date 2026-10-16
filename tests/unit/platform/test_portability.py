"""Running anywhere: the folder Nox owns per OS, secrets without a keyring, headless supervisors,
workers that die with their core."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from nox.paths import nox_home, user_config_path
from nox.security.file_secrets import FileSecretStore
from nox.security.secret_names import SecretNameError, SecretStoreError
from nox.security.secrets import keyring_usable, open_secret_store


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("NOX_HOME", "APPDATA", "XDG_DATA_HOME", "NOX_USER_CONFIG"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("win32", ("AppData", "Roaming", "Nox")),
        ("darwin", ("Library", "Application Support", "Nox")),
        ("linux", (".local", "share", "nox")),
    ],
)
def test_each_os_gets_its_own_place(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str, expected: tuple[str, ...]
) -> None:
    _clear(monkeypatch)
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert nox_home() == tmp_path.joinpath(*expected)
    assert user_config_path() == tmp_path.joinpath(*expected, "user.yaml")


def test_nox_home_wins_then_xdg_then_appdata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert nox_home() == tmp_path / "xdg" / "nox"
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    assert nox_home() == tmp_path / "appdata" / "Nox"
    monkeypatch.setenv("NOX_HOME", str(tmp_path / "srv"))
    assert nox_home() == tmp_path / "srv"


def test_shipped_defaults_follow_nox_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from nox.core.config import load_config
    from nox.paths import defaults_path

    _clear(monkeypatch)
    monkeypatch.setenv("NOX_HOME", str(tmp_path / "srv"))
    cfg = load_config(defaults_path(), None)
    assert cfg.paths.data_dir == tmp_path / "srv"
    assert cfg.paths.runtime_dir == tmp_path / "srv" / "runtime"


def test_the_file_store_keeps_secrets_owner_only_and_atomic(tmp_path: Path) -> None:
    store = FileSecretStore(tmp_path)
    assert store.get("nox/calendar/google_ics_url") is None
    store.set("nox/calendar/google_ics_url", "https://example.invalid/basic.ics")
    store.set("nox/home/access_token", "t0k3n")
    assert store.get("nox/home/access_token") == "t0k3n"
    assert store.names() == ["nox/calendar/google_ics_url", "nox/home/access_token"]
    store.delete("nox/home/access_token")
    store.delete("nox/home/access_token")  # deleting twice is fine
    data = json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8"))
    assert data == {
        "version": 1,
        "secrets": {"nox/calendar/google_ics_url": "https://example.invalid/basic.ics"},
    }
    assert not (tmp_path / "secrets.json.tmp").exists()
    if sys.platform != "win32":
        assert (tmp_path / "secrets.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(SecretNameError):
        store.set("not a name", "x")
    assert "t0k3n" not in repr(store)


def test_a_foreign_file_is_refused_not_overwritten(tmp_path: Path) -> None:
    (tmp_path / "secrets.json").write_text('{"something": "else"}', encoding="utf-8")
    store = FileSecretStore(tmp_path)
    with pytest.raises(SecretStoreError, match="not a Nox secrets file"):
        store.set("nox/home/access_token", "x")
    assert json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8")) == {
        "something": "else"
    }


def test_a_keyring_without_a_backend_is_recognised() -> None:
    def kr(backend: object) -> SimpleNamespace:
        return SimpleNamespace(get_keyring=lambda: backend)

    assert keyring_usable(kr(SimpleNamespace(priority=5))) is True
    assert keyring_usable(kr(SimpleNamespace(priority=0))) is False  # keyring.backends.fail
    assert keyring_usable(kr(SimpleNamespace(priority=10, backends=[]))) is False  # empty chainer

    def broken() -> object:
        raise RuntimeError("no dbus")

    assert keyring_usable(SimpleNamespace(get_keyring=broken)) is False


def test_auto_falls_back_to_the_file_where_no_keyring_works(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import nox.security.secrets as secrets_module

    monkeypatch.setattr(secrets_module, "keyring_usable", lambda module=None: False)
    store = open_secret_store("auto", home=tmp_path)
    assert isinstance(store, FileSecretStore) and store.path == tmp_path / "secrets.json"
    assert isinstance(open_secret_store("file", home=tmp_path), FileSecretStore)


def test_a_headless_linux_has_no_display(monkeypatch: pytest.MonkeyPatch) -> None:
    from nox.supervisor.main import has_display

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert has_display() is False
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert has_display() is True
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("WAYLAND_DISPLAY")
    assert has_display() is True


def test_a_headless_supervisor_starts_no_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    import nox.supervisor.main as sup
    from nox.core.config import NoxConfig

    monkeypatch.setattr(sup, "has_display", lambda: False)
    settings = sup.SupervisorSettings.from_config(NoxConfig())
    assert settings.shell_command is None and settings.hotkey_enabled is False
    monkeypatch.setattr(sup, "has_display", lambda: True)
    assert sup.SupervisorSettings.from_config(NoxConfig()).shell_command is not None


@pytest.mark.skipif(sys.platform == "win32", reason="the job object covers Windows")
def test_a_worker_dies_with_its_parent(tmp_path: Path) -> None:
    import subprocess
    import time

    marker = tmp_path / "child.pid"
    child = tmp_path / "child.py"
    child.write_text(
        "import os, sys, time\n"
        "from nox.core.jobobject import die_with_parent\n"
        "die_with_parent(poll_s=0.1)\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import os, subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
        "deadline = time.monotonic() + 30\n"
        "while not os.path.exists(sys.argv[2]) and time.monotonic() < deadline:\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    subprocess.run(
        [sys.executable, str(parent), str(child), str(marker)], check=True, env=env, timeout=60
    )
    pid = int(marker.read_text())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not _alive(pid):
            return
        time.sleep(0.1)
    pytest.fail(f"worker {pid} outlived its parent")


def _alive(pid: int) -> bool:
    """Running, and not a zombie waiting for a parent that will never reap it."""
    import psutil

    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False

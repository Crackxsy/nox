"""Suite-wide isolation: no test may read or write the developer's own Nox installation or the
operating system's credential store."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import keyring
import keyring.errors
import pytest
from keyring.backend import KeyringBackend

import nox.sensors.install as sensors_install
from nox.sensors.probe import ForegroundInfo, ProbeSelection


@pytest.fixture(autouse=True)
def _isolated_app_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    """Point `nox.paths.app_dir` at a fresh temporary folder for every test.

    Without it, any code path that falls back to the default location - `%APPDATA%\\Nox`,
    `~/Library/Application Support/Nox` or `~/.local/share/nox` - touches the real one on the
    machine running the tests. A test that needs the platform default removes `NOX_APP_DIR`.
    """
    app = tmp_path_factory.mktemp("nox_app_dir")
    monkeypatch.setenv("NOX_APP_DIR", str(app))
    monkeypatch.delenv("NOX_USER_CONFIG", raising=False)
    monkeypatch.delenv("NOX_RUNTIME_DIR", raising=False)
    return app


class MemoryKeyring(KeyringBackend):
    """A process-local keyring: what the Windows Credential Manager, the macOS Keychain or a Linux
    Secret Service would hold, without touching any of them."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.entries: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.entries.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.entries[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if self.entries.pop((service, username), None) is None:
            raise keyring.errors.PasswordDeleteError(username)


@pytest.fixture(autouse=True)
def isolated_keyring() -> Iterator[MemoryKeyring]:
    """Every test gets an empty keyring of its own.

    The core builds its secret store from the `keyring` package. Without this, an integration test
    read the developer's real credential store - a configured PIN changed how the tests behaved -
    and on a machine without one (a Linux CI runner) the core could not boot at all.
    """
    previous = keyring.get_keyring()
    memory = MemoryKeyring()
    keyring.set_keyring(memory)
    yield memory
    keyring.set_keyring(previous)


class QuietDesktopProbe:
    """A desktop with nothing in front and nobody idle: the neutral state every test starts from.

    The real probe reads the machine running the tests - whatever window happens to be focused on
    a developer's screen or a CI runner, or nothing at all on a headless one, which is (correctly)
    a fail-closed privacy zone. A test about zones swaps in its own probe.
    """

    def foreground(self) -> ForegroundInfo:
        return ForegroundInfo("", "", 0)

    def idle_seconds(self) -> float:
        return 0.0


@pytest.fixture(autouse=True)
def quiet_desktop(monkeypatch: pytest.MonkeyPatch) -> QuietDesktopProbe:
    """Sensors installed by any test read `QuietDesktopProbe`, never the host's desktop."""
    probe = QuietDesktopProbe()
    monkeypatch.setattr(sensors_install, "select_probe", lambda: ProbeSelection(probe))
    return probe

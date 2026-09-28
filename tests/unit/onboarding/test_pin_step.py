"""`nox pin set|clear|status` and the refusal of `nox secrets set nox/security/pin`.

The commands run against an in-memory credential store here; the real one is never touched.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner

import nox.onboarding.pin_step as pin_step
from nox.cli import app
from nox.security.pin_attempts import SqlitePinAttemptStore
from nox.security.secrets import PIN_SECRET_NAME, InMemorySecretStore, PinManager

runner = CliRunner()
PIN = "471108"
OTHER = "902211"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> InMemorySecretStore:
    memory = InMemorySecretStore()

    @contextmanager
    def fake_manager(_database_dir: Path | None) -> Iterator[PinManager]:
        yield PinManager(memory)

    monkeypatch.setattr(pin_step, "local_pin_manager", fake_manager)
    monkeypatch.setattr("nox.cli._pin_database_dir", lambda: None)
    return memory


def _pin(store: InMemorySecretStore) -> PinManager:
    return PinManager(store)


def test_pin_set_asks_twice_and_stores_a_hash(store: InMemorySecretStore) -> None:
    result = runner.invoke(app, ["pin", "set"], input=f"{PIN}\n{PIN}\n")

    assert result.exit_code == 0, result.output
    assert "PIN set" in result.output
    assert _pin(store).verify_pin(PIN).ok
    assert PIN not in result.output  # hidden prompt: never echoed


def test_pin_set_refuses_a_short_pin_and_a_mismatch(store: InMemorySecretStore) -> None:
    result = runner.invoke(app, ["pin", "set"], input=f"1234\n{PIN}\n{OTHER}\n{PIN}\n{PIN}\n")

    assert result.exit_code == 0, result.output
    assert "at least 6 characters" in result.output
    assert "differ" in result.output
    assert _pin(store).verify_pin(PIN).ok


def test_changing_the_pin_needs_the_current_one(store: InMemorySecretStore) -> None:
    _pin(store).set_pin(PIN)

    wrong = runner.invoke(app, ["pin", "set"], input=f"000000\n{OTHER}\n{OTHER}\n")
    assert wrong.exit_code == 1
    assert "wrong PIN" in wrong.output
    assert _pin(store).verify_pin(PIN).ok

    right = runner.invoke(app, ["pin", "set"], input=f"{PIN}\n{OTHER}\n{OTHER}\n")
    assert right.exit_code == 0, right.output
    assert _pin(store).verify_pin(OTHER).ok


def test_pin_clear_needs_the_current_one(store: InMemorySecretStore) -> None:
    _pin(store).set_pin(PIN)

    assert runner.invoke(app, ["pin", "clear"], input="000000\n").exit_code == 1
    assert store.get(PIN_SECRET_NAME) is not None

    result = runner.invoke(app, ["pin", "clear"], input=f"{PIN}\n")
    assert result.exit_code == 0 and "PIN removed" in result.output
    assert store.get(PIN_SECRET_NAME) is None


def test_the_command_line_repairs_a_raw_entry(store: InMemorySecretStore) -> None:
    """The dashboard refuses to touch a raw entry; the local command line is the way out."""
    store.set(PIN_SECRET_NAME, PIN)

    status = runner.invoke(app, ["pin", "status"])
    assert status.exit_code == 1 and "not a Nox PIN hash" in status.output

    result = runner.invoke(app, ["pin", "set"], input=f"{OTHER}\n{OTHER}\n")
    assert result.exit_code == 0, result.output
    assert "will be replaced" in result.output
    assert _pin(store).verify_pin(OTHER).ok


def test_pin_status_never_shows_the_pin(store: InMemorySecretStore) -> None:
    assert runner.invoke(app, ["pin", "status"]).output.strip() == "not set"
    _pin(store).set_pin(PIN)

    result = runner.invoke(app, ["pin", "status"])

    assert result.exit_code == 0 and result.output.strip() == "set"
    assert PIN not in result.output and "argon2" not in result.output


def test_secrets_set_refuses_the_pin_entry_and_points_to_nox_pin_set() -> None:
    result = runner.invoke(app, ["secrets", "set", PIN_SECRET_NAME], input=f"{PIN}\n")

    assert result.exit_code == 2
    assert "nox pin set" in result.output


def test_failed_attempts_are_counted_in_the_nox_database(tmp_path: Path) -> None:
    """The lockout the core enforces also holds for guesses typed at the command line."""
    import sqlite3

    database_dir = tmp_path / "database"
    database_dir.mkdir()
    sqlite3.connect(database_dir / pin_step.DATABASE_FILENAME).close()

    with pin_step.local_pin_manager(database_dir) as pin:
        assert isinstance(pin._attempts, SqlitePinAttemptStore)
    with pin_step.local_pin_manager(tmp_path / "missing") as pin:
        assert not isinstance(pin._attempts, SqlitePinAttemptStore)

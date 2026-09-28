"""nox.data.security_repos: the one-row security state survives a reopen, and a damaged row is
reported as unreadable - never as "nothing stored"."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from nox.core.state import PrivacyMode
from nox.data.db import Database
from nox.data.security_repos import SecurityStateRepository
from nox.security.persisted_state import PersistedSecurityState, SecurityStateUnreadableError


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "nox.db")
    database.migrate()
    yield database
    database.close()


def test_nothing_stored_on_a_fresh_database(db: Database) -> None:
    assert SecurityStateRepository(db).load() is None


def test_state_round_trips_across_a_reopen(tmp_path: Path) -> None:
    path = tmp_path / "nox.db"
    first = Database(path)
    first.migrate()
    state = PersistedSecurityState(
        privacy_mode=PrivacyMode.OFFLINE,
        panic=True,
        kill_engaged=True,
        kill_security_path=True,
        kill_origin="panic",
        kill_reason="panic",
    )
    SecurityStateRepository(first).save(state)
    first.close()

    second = Database(path)
    second.migrate()
    try:
        assert SecurityStateRepository(second).load() == state
    finally:
        second.close()


def test_save_overwrites_the_single_row(db: Database) -> None:
    repo = SecurityStateRepository(db)
    repo.save(PersistedSecurityState(privacy_mode=PrivacyMode.PRIVATE))
    repo.save(PersistedSecurityState(privacy_mode=PrivacyMode.BALANCED))
    assert db.fetch_one("SELECT COUNT(*) AS n FROM security_state")["n"] == 1  # type: ignore[index]
    loaded = repo.load()
    assert loaded is not None and loaded.privacy_mode is PrivacyMode.BALANCED


def test_an_invalid_row_is_unreadable_and_keeps_the_mode_it_could_read(db: Database) -> None:
    db.execute(
        "INSERT INTO security_state (id, privacy_mode, panic, kill_engaged, updated_at)"
        " VALUES (1, 'private', 7, 0, '2026-09-28T00:00:00+00:00')"
    )
    with pytest.raises(SecurityStateUnreadableError) as caught:
        SecurityStateRepository(db).load()
    assert caught.value.privacy_mode is PrivacyMode.PRIVATE


def test_an_unknown_mode_is_unreadable_without_a_mode(db: Database) -> None:
    db.execute(
        "INSERT INTO security_state (id, privacy_mode, updated_at)"
        " VALUES (1, 'party', '2026-09-28T00:00:00+00:00')"
    )
    with pytest.raises(SecurityStateUnreadableError) as caught:
        SecurityStateRepository(db).load()
    assert caught.value.privacy_mode is None


def test_a_missing_table_is_unreadable_not_empty(tmp_path: Path) -> None:
    database = Database(tmp_path / "unmigrated.db")
    try:
        with pytest.raises(SecurityStateUnreadableError):
            SecurityStateRepository(database).load()
    finally:
        database.close()

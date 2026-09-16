"""nox.data.rl_repos: typed rows over rl_matches/rl_events/rl_replays (Spec v0.3 §7, migration
0003_rl.sql)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from nox.data.db import Database
from nox.data.rl_repos import RlEventRepository, RlMatchRepository, RlReplayRepository


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "nox.db")
    database.migrate()
    yield database
    database.close()


def test_match_lifecycle(db: Database) -> None:
    repo = RlMatchRepository(db)
    row = repo.start()
    assert row.ended_at is None
    ok = repo.end(row.id, score_self=3, score_opponent=1, result="win", summary_short="Win 3-1.")
    assert ok is True
    fetched = repo.get(row.id)
    assert fetched is not None
    assert fetched.result == "win"
    assert fetched.summary_short == "Win 3-1."
    assert repo.active() is None


def test_replay_upsert_is_idempotent_by_file_path(db: Database) -> None:
    repo = RlReplayRepository(db)
    first = repo.upsert(
        file_path="C:/replays/a.replay",
        file_hash="",
        parser_version="0.1.0",
        parse_status="ok",
        header={"map": "cs_p"},
    )
    second = repo.upsert(
        file_path="C:/replays/a.replay",
        file_hash="",
        parser_version="0.1.0",
        parse_status="ok",
        header={"map": "cs_p", "team_size": 2},
    )
    assert first.id == second.id
    assert repo.get(first.id).header_json != "{}"


def test_replay_matched_to_match(db: Database) -> None:
    matches = RlMatchRepository(db)
    replays = RlReplayRepository(db)
    match = matches.start()
    replay = replays.upsert(
        file_path="C:/replays/b.replay",
        file_hash="",
        parser_version="0.1.0",
        parse_status="ok",
        header={},
    )
    replays.set_matched_match(replay.id, match.id)
    assert replays.get(replay.id).matched_match_id == match.id


def test_event_add_and_list_for_match(db: Database) -> None:
    matches = RlMatchRepository(db)
    events = RlEventRepository(db)
    match = matches.start()
    events.add(
        match_id=match.id, kind="boost_low", source="hud", confidence=0.9, payload={"boost": 15}
    )
    events.add(
        match_id=match.id, kind="goal", source="hud", confidence=0.95, payload={"team": "self"}
    )
    rows = events.list_for_match(match.id)
    assert [r.kind for r in rows] == ["boost_low", "goal"]

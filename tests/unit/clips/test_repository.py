"""`ClipRepository`/`ClipMarkerRepository` over the real migration `0006_clips.sql` (ST-15-01
acceptance criteria: insert/read/status-transition operations are covered by unit tests)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nox.clips.repository import ClipMarkerRepository, ClipRepository
from nox.data.db import Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    database.migrate()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def repo(db: Database) -> ClipRepository:
    return ClipRepository(db)


@pytest.fixture
def markers(db: Database) -> ClipMarkerRepository:
    return ClipMarkerRepository(db)


def test_insert_and_get_round_trip(repo: ClipRepository) -> None:
    row = repo.insert(
        source="event",
        trigger_kind="rl.goal",
        file_path=r"E:\Nox\data\clips\library\goal.mp4",
        session_id="s1",
        duration_s=12.5,
        tags=["goal", "hype"],
        checksum="abc123",
    )
    assert row.status == "new"
    fetched = repo.get(row.id)
    assert fetched is not None
    assert fetched.file_path == r"E:\Nox\data\clips\library\goal.mp4"
    assert fetched.tags == ["goal", "hype"]
    assert fetched.duration_s == 12.5


def test_get_by_checksum_dedups(repo: ClipRepository) -> None:
    row = repo.insert(source="event", trigger_kind="rl.goal", file_path="a.mp4", checksum="same")
    found = repo.get_by_checksum("same")
    assert found is not None
    assert found.id == row.id
    assert repo.get_by_checksum("missing") is None
    assert repo.get_by_checksum("") is None


def test_list_clips_filters_by_status(repo: ClipRepository) -> None:
    a = repo.insert(source="event", trigger_kind="rl.goal", file_path="a.mp4")
    b = repo.insert(source="manual", trigger_kind="user_marker", file_path="b.mp4")
    repo.set_status(b.id, "exported")
    new_only = repo.list_clips(status="new")
    assert [r.id for r in new_only] == [a.id]
    everything = repo.list_clips()
    assert {r.id for r in everything} == {a.id, b.id}


def test_set_status_transitions(repo: ClipRepository) -> None:
    row = repo.insert(source="event", trigger_kind="rl.goal", file_path="a.mp4")
    assert repo.set_status(row.id, "reviewed") is True
    assert repo.get(row.id).status == "reviewed"
    assert repo.set_status("missing-id", "reviewed") is False


def test_set_status_rejects_unknown_status(repo: ClipRepository) -> None:
    row = repo.insert(source="event", trigger_kind="rl.goal", file_path="a.mp4")
    with pytest.raises(ValueError, match="unknown clip status"):
        repo.set_status(row.id, "bogus")


def test_update_tags_and_notes(repo: ClipRepository) -> None:
    row = repo.insert(source="event", trigger_kind="rl.goal", file_path="a.mp4", tags=["x"])
    assert repo.update_tags(row.id, tags=["y", "z"], notes="hello") is True
    fetched = repo.get(row.id)
    assert fetched.tags == ["y", "z"]
    assert fetched.notes == "hello"
    # notes omitted -> unchanged
    assert repo.update_tags(row.id, tags=["only"]) is True
    assert repo.get(row.id).notes == "hello"


def test_add_tags_deduplicates(repo: ClipRepository) -> None:
    row = repo.insert(source="event", trigger_kind="rl.goal", file_path="a.mp4", tags=["a"])
    assert repo.add_tags(row.id, ["b", "a", "c"]) is True
    assert repo.get(row.id).tags == ["a", "b", "c"]


def test_parent_child_trim_linkage(repo: ClipRepository) -> None:
    parent = repo.insert(source="event", trigger_kind="rl.goal", file_path="a.mp4")
    child = repo.insert(
        source="event",
        trigger_kind="rl.goal",
        file_path="a_trim.mp4",
        parent_clip_id=parent.id,
    )
    assert repo.get(child.id).parent_clip_id == parent.id


def test_clip_markers_insert_and_list(markers: ClipMarkerRepository) -> None:
    row = markers.insert(session_id="s1", timestamp_s=154.18, reason="RL Goal - high chat hype")
    assert row.promoted_clip_id is None
    listed = markers.list_markers(session_id="s1")
    assert [m.id for m in listed] == [row.id]


def test_clip_markers_promote(markers: ClipMarkerRepository, repo: ClipRepository) -> None:
    marker = markers.insert(session_id="s1", timestamp_s=10.0, reason="test")
    clip = repo.insert(source="marker_promoted", trigger_kind="user_marker", file_path="a.mp4")
    assert markers.promote(marker.id, clip.id) is True
    assert markers.get(marker.id).promoted_clip_id == clip.id

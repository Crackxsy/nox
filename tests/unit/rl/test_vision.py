"""Core-side Vision Stage 2 (`nox.rl.vision`): rough rotation analysis over synthetic detection
sequences (pure function), `rl.vision.detections` persistence against the active match, and the
post-match analysis service's skip-if-no-frames / persist-and-speak-if-frames behaviour (Spec v0.9
§4.5/§7, ST-18-06)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nox.core.bus import AsyncEventBus
from nox.core.events import E, Event
from nox.data.db import Database
from nox.data.rl_repos import RlMatchRepository
from nox.data.rl_vision_repos import RlVisionAnalysisRepository, RlVisionFrameRepository
from nox.rl.vision import (
    RlVisionAnalysisService,
    RlVisionPersistenceService,
    compute_rotation_analysis,
)
from nox.voice.base import TtsRequest


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "nox.db")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def bus() -> AsyncEventBus:
    return AsyncEventBus()


class FakeSpeaker:
    def __init__(self) -> None:
        self.said: list[TtsRequest] = []

    async def say(self, request: TtsRequest) -> None:
        self.said.append(request)


@dataclass(slots=True)
class _Frame:
    ts: datetime
    entity: str
    team: str | None
    x: float
    y: float
    w: float
    h: float


def _t(seconds: float) -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=seconds)


# ---- compute_rotation_analysis (pure function) --------------------------------------------------


def test_rotation_analysis_on_empty_sequence_produces_no_signal() -> None:
    result = compute_rotation_analysis([])
    assert result.frames_analyzed == 0
    assert result.ball_side_ratio is None
    assert result.avg_self_car_ball_distance is None
    assert result.coaching_summary == ""


def test_ball_side_ratio_reflects_which_half_the_ball_was_seen_on() -> None:
    frames = [
        _Frame(_t(0), "ball", None, 0.1, 0.5, 0.02, 0.02),  # self half
        _Frame(_t(1), "ball", None, 0.15, 0.5, 0.02, 0.02),  # self half
        _Frame(_t(2), "ball", None, 0.9, 0.5, 0.02, 0.02),  # opponent half
    ]
    result = compute_rotation_analysis(frames)
    assert result.frames_analyzed == 3
    assert result.ball_side_ratio == pytest.approx(2 / 3)
    assert "ball on your half" in result.coaching_summary


def test_avg_self_car_ball_distance_pairs_close_in_time_samples_only() -> None:
    frames = [
        _Frame(_t(0), "ball", None, 0.5, 0.5, 0.02, 0.02),
        _Frame(_t(0.5), "car", "self", 0.5, 0.5, 0.02, 0.02),  # same spot, close in time
        _Frame(_t(0), "car", "opponent", 0.0, 0.0, 0.02, 0.02),  # ignored: not self team
        _Frame(_t(500), "car", "self", 0.0, 0.0, 0.02, 0.02),  # far in time, never paired
    ]
    result = compute_rotation_analysis(frames)
    assert result.avg_self_car_ball_distance == pytest.approx(0.0, abs=1e-6)
    assert "distance to the ball" in result.coaching_summary


def test_coaching_summary_is_empty_when_only_opponent_cars_seen() -> None:
    frames = [_Frame(_t(0), "car", "opponent", 0.5, 0.5, 0.02, 0.02)]
    result = compute_rotation_analysis(frames)
    assert result.frames_analyzed == 1
    assert result.ball_side_ratio is None
    assert result.avg_self_car_ball_distance is None
    assert result.coaching_summary == ""  # no fabricated insight from opponent-only data (P10)


# ---- persistence ----------------------------------------------------------------------------


async def test_persistence_service_writes_frames_against_the_active_match(
    bus: AsyncEventBus, db: Database
) -> None:
    matches = RlMatchRepository(db)
    frames_repo = RlVisionFrameRepository(db)
    service = RlVisionPersistenceService(bus, frames_repo, matches, retain_hours=1.0)
    service.start()

    match = matches.start()
    await bus.publish(
        Event(
            name=E.RL_VISION_DETECTIONS,
            payload={
                "backend": "opencv",
                "detections": [
                    {"entity": "ball", "confidence": 0.8, "x": 0.5, "y": 0.5, "w": 0.02, "h": 0.02}
                ],
            },
        )
    )

    stored = frames_repo.list_for_match(match.id)
    assert len(stored) == 1
    assert stored[0].entity == "ball"
    assert stored[0].backend == "opencv"
    assert stored[0].retain_until is not None


async def test_persistence_service_stores_orphan_detections_with_no_active_match(
    bus: AsyncEventBus, db: Database
) -> None:
    matches = RlMatchRepository(db)
    frames_repo = RlVisionFrameRepository(db)
    service = RlVisionPersistenceService(bus, frames_repo, matches)
    service.start()

    await bus.publish(
        Event(
            name=E.RL_VISION_DETECTIONS,
            payload={
                "backend": "opencv",
                "detections": [
                    {
                        "entity": "car",
                        "confidence": 0.6,
                        "x": 0.1,
                        "y": 0.1,
                        "w": 0.02,
                        "h": 0.02,
                        "team": "self",
                    }
                ],
            },
        )
    )
    row = frames_repo.get(1)
    assert row is not None
    assert row.match_id is None  # no active match: written, never dropped


# ---- post-match analysis ---------------------------------------------------------------------


async def test_analysis_service_skips_a_match_with_no_frames(
    bus: AsyncEventBus, db: Database
) -> None:
    matches = RlMatchRepository(db)
    frames_repo = RlVisionFrameRepository(db)
    analysis_repo = RlVisionAnalysisRepository(db)
    speaker = FakeSpeaker()
    match = matches.start()
    matches.end(match.id, result="win", summary_short="Win 3-1.")

    service = RlVisionAnalysisService(bus, frames_repo, analysis_repo, matches, speaker=speaker)
    service.start()
    seen: list[Event] = []
    bus.subscribe(E.RL_VISION_ANALYSIS, lambda ev: seen.append(ev))

    await bus.publish(Event(name=E.RL_MATCH_ENDED, payload={"match_id": 1}))

    assert seen == []
    assert speaker.said == []
    assert analysis_repo.list_for_match(match.id) == []


async def test_analysis_service_persists_speaks_and_publishes_when_frames_exist(
    bus: AsyncEventBus, db: Database
) -> None:
    matches = RlMatchRepository(db)
    frames_repo = RlVisionFrameRepository(db)
    analysis_repo = RlVisionAnalysisRepository(db)
    speaker = FakeSpeaker()
    match = matches.start()
    frames_repo.add(match_id=match.id, entity="ball", confidence=0.8, x=0.1, y=0.5, w=0.02, h=0.02)
    frames_repo.add(match_id=match.id, entity="ball", confidence=0.8, x=0.2, y=0.5, w=0.02, h=0.02)
    matches.end(match.id, result="win", summary_short="Win 3-1.")

    service = RlVisionAnalysisService(bus, frames_repo, analysis_repo, matches, speaker=speaker)
    service.start()
    seen: list[Event] = []
    bus.subscribe(E.RL_VISION_ANALYSIS, lambda ev: seen.append(ev))

    await bus.publish(Event(name=E.RL_MATCH_ENDED, payload={"match_id": 1}))

    assert len(seen) == 1
    assert seen[0].payload["match_id"] == match.id
    assert seen[0].payload["frames_analyzed"] == 2
    stored = analysis_repo.list_for_match(match.id)
    assert len(stored) == 1
    assert stored[0].coaching_summary
    assert speaker.said and speaker.said[0].text == stored[0].coaching_summary

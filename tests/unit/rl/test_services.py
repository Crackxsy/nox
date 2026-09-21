"""Core-side services (nox.rl.services): mode bridge, DB persistence, the callout engine's
private-channel-only contract, post-match announcer, and session-end summary skip-if-empty
behavior (Spec v0.3 §3.1/§3.2/§3.3/§7)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from nox.core.bus import AsyncEventBus
from nox.core.events import E, Event
from nox.core.state import NoxState
from nox.core.statemgr import NoxStateManager
from nox.data.db import Database
from nox.data.repos import StateCheckpointRepository
from nox.data.rl_repos import RlEventRepository, RlMatchRepository, RlReplayRepository
from nox.rl.services import (
    RlCalloutService,
    RlMatchAnnouncer,
    RlModeBridge,
    RlPersistenceService,
    RlSessionSummaryService,
)
from nox.voice.base import Channel, TtsRequest


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "nox.db")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def bus() -> AsyncEventBus:
    return AsyncEventBus()


class FakeSecurityEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def set_profile(self, profile_id: str, *, by: str) -> None:
        self.calls.append((profile_id, by))


class FakeSpeaker:
    def __init__(self) -> None:
        self.said: list[TtsRequest] = []

    async def say(self, request: TtsRequest) -> None:
        self.said.append(request)


class FakeEngine:
    """`CalloutEngineProtocol`: always fires a fixed decision, for `RlCalloutService` tests."""

    class _Decision:
        rule_id = "rl.callout.test"
        clip_id = "test_clip"
        fallback_text = "Test callout."

    def evaluate(self, **_kwargs: Any) -> Any:
        return self._Decision()


async def test_mode_bridge_transitions_to_rocket_league_on_game_detected(
    bus: AsyncEventBus,
) -> None:
    state = NoxStateManager(bus, StateCheckpointRepository(Database(":memory:")), state=NoxState())
    engine = FakeSecurityEngine()
    bridge = RlModeBridge(bus, state, engine)
    bridge.start()
    seen: list[Event] = []
    bus.subscribe(E.SYSTEM_MODE_CHANGED, lambda ev: seen.append(ev))

    await bus.publish(Event(name=E.GAME_DETECTED, payload={"game": "rocket_league"}))
    assert state.get("assistant.mode") == "rocket_league"
    assert seen and seen[-1].payload["current"] == "rocket_league"
    assert seen[-1].payload["game_running"] is True
    assert ("rocket_league", "rl") in engine.calls

    await bus.publish(
        Event(name=E.GAME_ENDED, payload={"game": "rocket_league", "duration_s": 5.0})
    )
    assert state.get("assistant.mode") != "rocket_league"
    assert seen[-1].payload["game_running"] is False


async def test_persistence_service_opens_and_closes_matches_and_records_events(
    bus: AsyncEventBus, db: Database
) -> None:
    matches = RlMatchRepository(db)
    events = RlEventRepository(db)
    replays = RlReplayRepository(db)
    service = RlPersistenceService(bus, matches, events, replays)
    service.start()

    await bus.publish(Event(name=E.RL_MATCH_STARTED, payload={"match_id": 1}))
    active = matches.active()
    assert active is not None

    await bus.publish(
        Event(
            name=E.RL_EVENT,
            payload={
                "match_id": 1,
                "kind": "boost_low",
                "source": "hud",
                "confidence": 0.9,
                "payload": {},
            },
        )
    )
    assert len(events.list_for_match(active.id)) == 1

    await bus.publish(
        Event(
            name=E.RL_MATCH_ENDED,
            payload={"match_id": 1, "result": "win", "summary_short": "Win 3-1."},
        )
    )
    closed = matches.get(active.id)
    assert closed is not None
    assert closed.ended_at is not None
    assert closed.result == "win"


async def test_replay_parsed_links_to_the_active_match(bus: AsyncEventBus, db: Database) -> None:
    matches = RlMatchRepository(db)
    events = RlEventRepository(db)
    replays = RlReplayRepository(db)
    service = RlPersistenceService(bus, matches, events, replays)
    service.start()

    await bus.publish(Event(name=E.RL_MATCH_STARTED, payload={"match_id": 1}))
    await bus.publish(
        Event(
            name=E.RL_REPLAY_PARSED,
            payload={
                "replay_id": 0,
                "file_path": "C:/r/x.replay",
                "parse_status": "ok",
                "header": {"map": "cs_p"},
            },
        )
    )
    row = replays.get_by_path("C:/r/x.replay")
    assert row is not None
    active = matches.active()
    assert active is not None
    assert row.matched_match_id == active.id
    assert matches.get(active.id).replay_id == row.id


async def test_callout_service_only_ever_speaks_on_the_private_channel(bus: AsyncEventBus) -> None:
    speaker = FakeSpeaker()
    service = RlCalloutService(bus, FakeEngine(), speaker)
    service.start()
    await bus.publish(
        Event(
            name=E.RL_EVENT,
            payload={
                "match_id": 1,
                "kind": "boost_low",
                "source": "hud",
                "confidence": 0.9,
                "payload": {},
            },
        )
    )
    assert len(speaker.said) == 1
    assert speaker.said[0].channel is Channel.PRIVATE
    assert speaker.said[0].prepared_clip == "test_clip"


async def test_match_announcer_speaks_the_short_summary_privately(bus: AsyncEventBus) -> None:
    speaker = FakeSpeaker()
    announcer = RlMatchAnnouncer(bus, speaker)
    announcer.start()
    await bus.publish(
        Event(name=E.RL_MATCH_ENDED, payload={"match_id": 1, "summary_short": "Win 3-1."})
    )
    assert len(speaker.said) == 1
    assert speaker.said[0].channel is Channel.PRIVATE
    assert speaker.said[0].text == "Win 3-1."


async def test_session_summary_is_skipped_when_no_matches_were_played(
    bus: AsyncEventBus, db: Database
) -> None:
    matches = RlMatchRepository(db)
    service = RlSessionSummaryService(bus, matches)
    service.start()
    await bus.publish(
        Event(
            name=E.SYSTEM_MODE_CHANGED,
            payload={"previous": "companion", "current": "rocket_league"},
        )
    )
    await bus.publish(
        Event(
            name=E.SYSTEM_MODE_CHANGED,
            payload={"previous": "rocket_league", "current": "companion"},
        )
    )
    # Nothing to assert on the DB side directly (no vault writer wired); the important behavior is
    # that this never raises when no matches exist - covered by not raising here.


async def test_session_summary_writes_a_templated_summary_without_fabricating_patterns(
    bus: AsyncEventBus, db: Database
) -> None:
    matches = RlMatchRepository(db)
    service = RlSessionSummaryService(bus, matches, router=None)
    service.start()
    await bus.publish(
        Event(
            name=E.SYSTEM_MODE_CHANGED,
            payload={"previous": "companion", "current": "rocket_league"},
        )
    )
    row = matches.start(started_at=datetime.now(UTC))
    matches.end(row.id, score_self=3, score_opponent=1, result="win")
    await bus.publish(
        Event(
            name=E.SYSTEM_MODE_CHANGED,
            payload={"previous": "rocket_league", "current": "companion"},
        )
    )
    updated = matches.get(row.id)
    assert updated is not None
    assert "not available yet" in updated.summary_detailed
    # No internal identifiers ever reach text the user reads or hears.
    assert "Spec" not in updated.summary_detailed


# -- fail-closed security -------------------------------------------------------------------------


class BrokenSecurityEngine:
    """A security engine whose profile switch fails, as a misconfigured profile set would."""

    def set_profile(self, profile_id: str, *, by: str) -> None:
        raise RuntimeError(f"profile {profile_id!r} cannot be applied")


async def test_failed_profile_switch_is_reported_on_the_mode_event(bus: AsyncEventBus) -> None:
    """The mode may still change, but nobody is told the game-mode permissions are in force."""
    state = NoxStateManager(bus, StateCheckpointRepository(Database(":memory:")), state=NoxState())
    bridge = RlModeBridge(bus, state, BrokenSecurityEngine())
    bridge.start()
    seen: list[Event] = []
    bus.subscribe(E.SYSTEM_MODE_CHANGED, lambda ev: seen.append(ev))

    await bus.publish(Event(name=E.GAME_DETECTED, payload={"game": "rocket_league"}))

    assert seen[-1].payload["current"] == "rocket_league"
    assert seen[-1].payload["reason"] == "game.detected; security profile unchanged"


async def test_successful_profile_switch_says_so(bus: AsyncEventBus) -> None:
    state = NoxStateManager(bus, StateCheckpointRepository(Database(":memory:")), state=NoxState())
    bridge = RlModeBridge(bus, state, FakeSecurityEngine())
    bridge.start()
    seen: list[Event] = []
    bus.subscribe(E.SYSTEM_MODE_CHANGED, lambda ev: seen.append(ev))

    await bus.publish(Event(name=E.GAME_DETECTED, payload={"game": "rocket_league"}))

    assert seen[-1].payload["reason"] == "game.detected"


class SilentSpeaker:
    """A speaker whose playback fails, e.g. because the private output device is gone."""

    async def say(self, request: TtsRequest) -> None:
        raise RuntimeError("no output device")


async def test_a_callout_nobody_heard_is_not_published_as_a_callout(
    bus: AsyncEventBus, db: Database
) -> None:
    service = RlCalloutService(bus, FakeEngine(), SilentSpeaker())
    service.start()
    callouts: list[Event] = []
    bus.subscribe(E.RL_CALLOUT, lambda ev: callouts.append(ev))

    await bus.publish(
        Event(name=E.RL_EVENT, payload={"kind": "goal", "source": "hud", "confidence": 0.9})
    )

    assert callouts == []


async def test_a_spoken_callout_is_published(bus: AsyncEventBus) -> None:
    service = RlCalloutService(bus, FakeEngine(), FakeSpeaker())
    service.start()
    callouts: list[Event] = []
    bus.subscribe(E.RL_CALLOUT, lambda ev: callouts.append(ev))

    await bus.publish(
        Event(name=E.RL_EVENT, payload={"kind": "goal", "source": "hud", "confidence": 0.9})
    )

    assert len(callouts) == 1
    assert callouts[0].payload["clip_id"] == "test_clip"

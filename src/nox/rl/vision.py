"""Core-side Vision Stage 2 (Spec v0.9 Vision Stage 2, EPIC-18, ST-18-01..06). A plugin worker has
no SQLite/TTS access, so - mirroring `nox.rl.services`'s pattern for Stage 1 - this module owns:

- `RlVisionPersistenceService`: `rl.vision.detections` -> `rl_vision_frames` (migration
  `0009_vision.sql`), short-retention rows (`rl.vision.detections_retain_hours`).
- `RlVisionAnalysisService`: `rl.match_ended` -> a rough, post-match rotation-position analysis over
  that match's stored frames (blended with the matched replay's header data where available) ->
  `rl.vision.analysis` and a short private spoken line queued right after the match (Personality v1
  B.9: "right after the match very short, at session end detailed" - the detailed path stays
  `RlSessionSummaryService`'s job, this module never duplicates it).

This is explicitly *rough*: a noisy signal from Stage 2's classical detector (Spec §3 "Not in
scope": no precise rotation, no shot prediction) - Stage 3's future replay-based analysis is a
separate, unbuilt flow (SP-17-gated). When a match produced no usable frames, nothing is persisted
and nothing is spoken - no fabricated insight (P10)."""

from __future__ import annotations

import contextlib
import uuid
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from nox.core.events import E, Event, EventBus
from nox.core.logging import get_logger
from nox.data.rl_repos import RlMatchRepository
from nox.data.rl_vision_repos import (
    RlVisionAnalysisRepository,
    RlVisionFrameRepository,
    default_retain_until_hours,
)
from nox.voice.base import Channel, TtsRequest

log = get_logger(__name__)


class Speaker(Protocol):
    async def say(self, request: TtsRequest) -> None: ...


# ---- persistence (ST-18-06) --------------------------------------------------------------------


class RlVisionPersistenceService:
    """`rl.vision.detections` -> `rl_vision_frames`. Resolves the DB match id via
    `RlMatchRepository.active()` at write time (a detection only ever arrives while `rl`'s vision
    loop believes a match is active - see `nox_plugin_rl.plugin.RlPlugin._vision_loop`); a
    detection that somehow arrives outside an active match is still stored with `match_id=None`
    rather than dropped, since Stage 2 metrics (ST-18-06) aggregate across matches too."""

    def __init__(
        self,
        bus: EventBus,
        frames: RlVisionFrameRepository,
        matches: RlMatchRepository,
        *,
        retain_hours: float = 6.0,
    ) -> None:
        self._bus = bus
        self._frames = frames
        self._matches = matches
        self._retain_hours = retain_hours
        self._unsub: Any = None

    def start(self) -> None:
        self._unsub = self._bus.subscribe(E.RL_VISION_DETECTIONS, self._on_detections)

    async def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def _on_detections(self, ev: Event) -> None:
        active = self._matches.active()
        match_id = active.id if active is not None else None
        backend = str(ev.payload.get("backend", "none"))
        retain_until = default_retain_until_hours(self._retain_hours)
        for detection in ev.payload.get("detections", []):
            self._frames.add(
                match_id=match_id,
                entity=str(detection.get("entity", "")),
                confidence=float(detection.get("confidence", 0.0)),
                x=float(detection.get("x", 0.0)),
                y=float(detection.get("y", 0.0)),
                w=float(detection.get("w", 0.0)),
                h=float(detection.get("h", 0.0)),
                team=detection.get("team"),
                backend=backend,
                retain_until=retain_until,
            )


# ---- rotation analysis (ST-18-06) ---------------------------------------------------------------


@runtime_checkable
class FrameLike(Protocol):
    ts: datetime
    entity: str
    team: str | None
    x: float
    y: float
    w: float
    h: float


class RotationAnalysis:
    """Plain result container (kept separate from the pydantic event payload so
    `compute_rotation_analysis` stays a pure, trivially unit-testable function)."""

    def __init__(
        self,
        *,
        frames_analyzed: int,
        ball_side_ratio: float | None,
        avg_self_car_ball_distance: float | None,
        coaching_summary: str,
    ) -> None:
        self.frames_analyzed = frames_analyzed
        self.ball_side_ratio = ball_side_ratio
        self.avg_self_car_ball_distance = avg_self_car_ball_distance
        self.coaching_summary = coaching_summary


def compute_rotation_analysis(frames: list[FrameLike]) -> RotationAnalysis:
    """Rough, honest rotation-position signal from a match's Stage-2 detections (Spec §3: never
    precise rotation, never shot prediction - Stage 3's future replay-based analysis is the real
    thing). Every metric is a best-effort approximation over noisy, independently-timestamped
    per-entity detections, not a synchronized multi-object track - documented here, and in the
    generated coaching text, rather than presented as more certain than it is (P10)."""
    # "frames_analyzed" counts stored detection rows (one `rl_vision_frames` row per detected
    # entity, not one row per captured video frame - a single sampled frame can yield zero to
    # several rows) - deliberately counts ALL of them, including entities the metrics below cannot
    # use (e.g. opponent-only cars), so a match that ran Stage 2 but had nothing worth saying is
    # still recorded as "ran, no signal" rather than indistinguishable from "never ran" (P10).
    frames_analyzed = len(frames)
    balls = [f for f in frames if f.entity == "ball"]
    self_cars = [f for f in frames if f.entity == "car" and f.team == "self"]

    ball_side_ratio: float | None = None
    if balls:
        on_self_half = sum(1 for b in balls if (b.x + b.w / 2.0) < 0.5)
        ball_side_ratio = on_self_half / len(balls)

    avg_distance: float | None = None
    if balls and self_cars:
        distances: list[float] = []
        for ball in balls:
            # Pair with the closest-in-time self-team car sample within a small window - frames
            # come from independent sample ticks, not a synchronized multi-object track, so this is
            # a rough association, not a guaranteed same-instant match.
            candidates = [c for c in self_cars if abs((c.ts - ball.ts).total_seconds()) <= 2.0]
            if not candidates:
                continue
            nearest = min(candidates, key=lambda c: abs((c.ts - ball.ts).total_seconds()))
            bx, by = ball.x + ball.w / 2.0, ball.y + ball.h / 2.0
            cx, cy = nearest.x + nearest.w / 2.0, nearest.y + nearest.h / 2.0
            distances.append(((bx - cx) ** 2 + (by - cy) ** 2) ** 0.5)
        if distances:
            avg_distance = sum(distances) / len(distances)

    return RotationAnalysis(
        frames_analyzed=frames_analyzed,
        ball_side_ratio=ball_side_ratio,
        avg_self_car_ball_distance=avg_distance,
        coaching_summary=_coaching_text(frames_analyzed, ball_side_ratio, avg_distance),
    )


def _coaching_text(
    frames_analyzed: int, ball_side_ratio: float | None, avg_distance: float | None
) -> str:
    """Personality v1 B.9: right after the match, very short - rather silent than wrong (D69)."""
    if frames_analyzed == 0:
        return ""
    bits: list[str] = []
    if ball_side_ratio is not None:
        bits.append(f"ball on your half {ball_side_ratio:.0%} of tracked frames")
    if avg_distance is not None:
        bits.append(f"avg. rough distance to the ball ~{avg_distance:.2f}")
    if not bits:
        return ""
    return "Rough vision read (experimental): " + ", ".join(bits) + "."


class RlVisionAnalysisService:
    """`rl.match_ended` -> `compute_rotation_analysis` over that match's stored frames ->
    `rl_vision_analysis` + `rl.vision.analysis` + a short private spoken line. Skipped entirely
    (no row, no event, no speech) when the match produced zero Stage-2 frames - no fabricated
    insight (P10), matching `RlSessionSummaryService`'s "skipped when no matches" pattern."""

    def __init__(
        self,
        bus: EventBus,
        frames: RlVisionFrameRepository,
        analysis: RlVisionAnalysisRepository,
        matches: RlMatchRepository,
        *,
        speaker: Speaker | None = None,
    ) -> None:
        self._bus = bus
        self._frames = frames
        self._analysis = analysis
        self._matches = matches
        self._speaker = speaker
        self._unsub: Any = None

    def start(self) -> None:
        self._unsub = self._bus.subscribe(E.RL_MATCH_ENDED, self._on_match_ended)

    async def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def _on_match_ended(self, _ev: Event) -> None:
        # The most recently *started* match is, by this plugin's one-match-at-a-time invariant,
        # the one that just ended - resolved this way (not via the plugin-local `match_id` in the
        # event payload) so this service never needs its own copy of `RlPersistenceService`'s
        # plugin-id -> db-id map, and never depends on cross-service event-dispatch ordering.
        recent = self._matches.list_recent(1)
        if not recent:
            return
        match = recent[0]
        frames = self._frames.list_for_match(match.id)
        result = compute_rotation_analysis(frames)  # type: ignore[arg-type]
        if result.frames_analyzed == 0:
            return
        self._analysis.add(
            match_id=match.id,
            frames_analyzed=result.frames_analyzed,
            ball_side_ratio=result.ball_side_ratio,
            avg_self_car_ball_distance=result.avg_self_car_ball_distance,
            coaching_summary=result.coaching_summary,
        )
        await self._bus.publish(
            Event(
                name=E.RL_VISION_ANALYSIS,
                payload={
                    "match_id": match.id,
                    "frames_analyzed": result.frames_analyzed,
                    "ball_side_ratio": result.ball_side_ratio,
                    "avg_self_car_ball_distance": result.avg_self_car_ball_distance,
                    "coaching_summary": result.coaching_summary,
                },
            )
        )
        if result.coaching_summary and self._speaker is not None:
            request = TtsRequest(
                utterance_id=uuid.uuid4().hex,
                text=result.coaching_summary,
                channel=Channel.PRIVATE,
            )
            with contextlib.suppress(Exception):
                await self._speaker.say(request)


# ---- composition ---------------------------------------------------------------------------------


class RlVisionRuntime:
    def __init__(
        self, persistence: RlVisionPersistenceService, analysis: RlVisionAnalysisService
    ) -> None:
        self.persistence = persistence
        self.analysis = analysis

    async def stop(self) -> None:
        await self.persistence.stop()
        await self.analysis.stop()


def install_vision(core: Any) -> RlVisionRuntime:
    """`core` is a started `nox.app.NoxCore` (or a test double exposing `config`/`db`/`bus`/
    `speaker`). Called from `nox.rl.install.install(core)`, guarded there so a Vision Stage 2
    wiring failure never breaks Stage 1."""
    matches = RlMatchRepository(core.db)
    frames = RlVisionFrameRepository(core.db)
    analysis_repo = RlVisionAnalysisRepository(core.db)
    retain_hours = float(getattr(core.config.rl.vision, "detections_retain_hours", 6.0))

    persistence = RlVisionPersistenceService(core.bus, frames, matches, retain_hours=retain_hours)
    persistence.start()

    speaker = getattr(core, "speaker", None)
    analysis = RlVisionAnalysisService(core.bus, frames, analysis_repo, matches, speaker=speaker)
    analysis.start()

    core.rl_vision_persistence = persistence
    core.rl_vision_analysis = analysis
    return RlVisionRuntime(persistence, analysis)


__all__ = [
    "RlVisionPersistenceService",
    "RlVisionAnalysisService",
    "RlVisionRuntime",
    "RotationAnalysis",
    "compute_rotation_analysis",
    "install_vision",
]

"""Core-side services for Rocket League Stage 1 (ST-12-01/06/07, Spec v0.3). A plugin worker has no
SQLite/TTS access (`nox.plugins.api.PluginApi` exposes only events/tools/state/secrets/egress), so
everything that needs the database, the voice layer or the AI router lives here and reacts to the
events `plugins/rl` emits over the bus. Wired by `nox.rl.install.install(core)`.
"""

from __future__ import annotations

import contextlib
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from nox.core.events import E, Event, EventBus
from nox.core.logging import get_logger
from nox.data.rl_repos import (
    RlEventRepository,
    RlMatchRepository,
    RlReplayRepository,
    default_retain_until,
)
from nox.voice.base import Channel, TtsRequest

log = get_logger(__name__)


class Speaker(Protocol):
    async def say(self, request: TtsRequest) -> None: ...


class ModeSetter(Protocol):
    """The subset of `SecurityEngine` the mode bridge needs."""

    def set_profile(self, profile_id: str, *, by: str) -> None: ...


# ---- mode bridge (ST-12-01) ------------------------------------------------------------------


class RlModeBridge:
    """`game.detected`/`game.ended` -> `system.mode_changed` (Spec §3.1). Mirrors the shape of
    `nox.app.NoxCore._h_mode_set` (mode + profile + event), just triggered by the plugin's
    detector instead of a dashboard/voice request."""

    def __init__(self, bus: EventBus, state: Any, security_engine: ModeSetter) -> None:
        self._bus = bus
        self._state = state
        self._security = security_engine
        self._unsubs: list[Any] = []
        self._previous_mode = "companion"

    def start(self) -> None:
        self._unsubs = [
            self._bus.subscribe(E.GAME_DETECTED, self._on_detected),
            self._bus.subscribe(E.GAME_ENDED, self._on_ended),
        ]

    async def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []

    async def _on_detected(self, ev: Event) -> None:
        if str(ev.payload.get("game", "")) != "rocket_league":
            return
        current = str(self._state.get("assistant.mode") or "companion")
        if current != "rocket_league":
            self._previous_mode = current
        await self._state.update("assistant.mode", "rocket_league", reason="game.detected")
        with contextlib.suppress(Exception):
            self._security.set_profile("rocket_league", by="rl")
        await self._bus.publish(
            Event(
                name=E.SYSTEM_MODE_CHANGED,
                payload={
                    "previous": self._previous_mode,
                    "current": "rocket_league",
                    "reason": "game.detected",
                    "game_running": True,
                },
            )
        )

    async def _on_ended(self, ev: Event) -> None:
        if str(ev.payload.get("game", "")) != "rocket_league":
            return
        await self._state.update("assistant.mode", self._previous_mode, reason="game.ended")
        with contextlib.suppress(Exception):
            self._security.set_profile("companion", by="rl")
        await self._bus.publish(
            Event(
                name=E.SYSTEM_MODE_CHANGED,
                payload={
                    "previous": "rocket_league",
                    "current": self._previous_mode,
                    "reason": "game.ended",
                    "game_running": False,
                },
            )
        )


# ---- persistence (ST-12-01/02/03/05) -----------------------------------------------------------


class RlPersistenceService:
    """`rl.match_started/ended`, `rl.event`, `rl.replay_parsed` -> `rl_matches`/`rl_events`/
    `rl_replays` (Spec §7). The plugin has no DB access; this is the only writer."""

    def __init__(
        self,
        bus: EventBus,
        matches: RlMatchRepository,
        events: RlEventRepository,
        replays: RlReplayRepository,
        *,
        events_retain_days: int = 180,
        matches_retain_days: int = 365,
    ) -> None:
        self._bus = bus
        self._matches = matches
        self._events = events
        self._replays = replays
        self._events_retain_days = events_retain_days
        self._matches_retain_days = matches_retain_days
        self._unsubs: list[Any] = []
        # plugin-local match_id (per process lifetime) -> db row id
        self._match_ids: dict[int, int] = {}

    def start(self) -> None:
        self._unsubs = [
            self._bus.subscribe(E.RL_MATCH_STARTED, self._on_match_started),
            self._bus.subscribe(E.RL_MATCH_ENDED, self._on_match_ended),
            self._bus.subscribe(E.RL_EVENT, self._on_event),
            self._bus.subscribe(E.RL_REPLAY_PARSED, self._on_replay_parsed),
        ]

    async def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []

    def _resolve_match_id(self, plugin_match_id: Any) -> int | None:
        if plugin_match_id is None:
            return None
        return self._match_ids.get(int(plugin_match_id))

    async def _on_match_started(self, ev: Event) -> None:
        plugin_id = ev.payload.get("match_id")
        row = self._matches.start()
        if plugin_id is not None:
            self._match_ids[int(plugin_id)] = row.id
        log.info("rl.match_opened", match_id=row.id)

    async def _on_match_ended(self, ev: Event) -> None:
        plugin_id = ev.payload.get("match_id")
        match_id = self._resolve_match_id(plugin_id)
        if match_id is None:
            return
        payload = ev.payload
        scores = payload.get("summary_short", "")
        self._matches.end(
            match_id,
            result=str(payload.get("result", "unknown")),
            summary_short=str(scores or payload.get("summary_short", "")),
            ended_reason="normal",
        )
        log.info("rl.match_closed", match_id=match_id)

    async def _on_event(self, ev: Event) -> None:
        payload = ev.payload
        match_id = self._resolve_match_id(payload.get("match_id"))
        retain_until = default_retain_until(self._events_retain_days)
        self._events.add(
            match_id=match_id,
            kind=str(payload.get("kind", "")),
            source=str(payload.get("source", "hud")),
            confidence=float(payload.get("confidence", 0.0)),
            payload=dict(payload.get("payload") or {}),
            retain_until=retain_until,
        )

    async def _on_replay_parsed(self, ev: Event) -> None:
        payload = ev.payload
        file_path = str(payload.get("file_path", ""))
        if not file_path:
            return
        header = dict(payload.get("header") or {})
        row = self._replays.upsert(
            file_path=file_path,
            file_hash="",
            parser_version=str(payload.get("parser_version", "")),
            parse_status=str(payload.get("parse_status", "failed")),
            header=header,
        )
        active = self._matches.active()
        if active is not None:
            self._replays.set_matched_match(row.id, active.id)
            self._matches.set_replay(active.id, row.id)


# ---- callout engine (ST-12-06) -----------------------------------------------------------------


class CalloutEngineProtocol(Protocol):
    def evaluate(
        self, *, kind: str, source: str, confidence: float, payload: dict[str, Any] | None = None
    ) -> Any: ...


class RlCalloutService:
    """`rl.event` -> the rule engine -> `TtsRequest(channel=PRIVATE, prepared_clip=...)` (Spec
    §3.2/§9, FR-10.3: never through text synthesis, private channel only). Measures the
    event-to-audio-start latency and emits `rl.callout` for transparency/debugging."""

    def __init__(
        self, bus: EventBus, engine: CalloutEngineProtocol, speaker: Speaker | None
    ) -> None:
        self._bus = bus
        self._engine = engine
        self._speaker = speaker
        self._unsub: Any = None

    def start(self) -> None:
        self._unsub = self._bus.subscribe(E.RL_EVENT, self._on_event)

    async def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def _on_event(self, ev: Event) -> None:
        payload = ev.payload
        decision = self._engine.evaluate(
            kind=str(payload.get("kind", "")),
            source=str(payload.get("source", "hud")),
            confidence=float(payload.get("confidence", 0.0)),
            payload=dict(payload.get("payload") or {}),
        )
        if decision is None:
            return
        t0 = time.monotonic()
        if self._speaker is not None:
            request = TtsRequest(
                utterance_id=uuid.uuid4().hex,
                text=decision.fallback_text,
                channel=Channel.PRIVATE,
                prepared_clip=decision.clip_id,
            )
            with contextlib.suppress(Exception):
                await self._speaker.say(request)
        latency_ms = (time.monotonic() - t0) * 1000.0
        await self._bus.publish(
            Event(
                name=E.RL_CALLOUT,
                payload={
                    "match_id": payload.get("match_id"),
                    "clip_id": decision.clip_id,
                    "rule_id": decision.rule_id,
                    "latency_ms": latency_ms,
                },
            )
        )


# ---- post-match short summary (ST-12-07) ---------------------------------------------------------


class RlMatchAnnouncer:
    """`rl.match_ended` -> a one-to-two-sentence private spoken summary within a few seconds
    (Spec §3.3 step 2: "never a full breakdown mid-session"). Goes through normal synthesis (not
    `prepared_clip` - the text is dynamic), still `Channel.PRIVATE` only."""

    def __init__(self, bus: EventBus, speaker: Speaker | None) -> None:
        self._bus = bus
        self._speaker = speaker
        self._unsub: Any = None

    def start(self) -> None:
        self._unsub = self._bus.subscribe(E.RL_MATCH_ENDED, self._on_match_ended)

    async def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def _on_match_ended(self, ev: Event) -> None:
        summary = str(ev.payload.get("summary_short", "")).strip()
        if not summary or self._speaker is None:
            return
        request = TtsRequest(utterance_id=uuid.uuid4().hex, text=summary, channel=Channel.PRIVATE)
        with contextlib.suppress(Exception):
            await self._speaker.say(request)


# ---- session summaries (ST-12-07) --------------------------------------------------------------


class AiRouterLike(Protocol):
    async def complete(self, request: Any) -> Any: ...


class VaultNoteWriter(Protocol):
    def write(self, *, session_started_at: datetime, text: str, match_ids: list[int]) -> str: ...


class RlSessionSummaryService:
    """Detailed session-end summary (Spec §3.3/§6.3): generated only after `rocket_league` mode has
    fully ended ("queue and execute after", the spec's own recommendation for the open §6.3
    question), never fabricates pattern-analysis it has no data for (FR-10.5 is out of v0.3 scope -
    §2.2/§15), and is skipped entirely when no matches were played."""

    def __init__(
        self,
        bus: EventBus,
        matches: RlMatchRepository,
        *,
        router: AiRouterLike | None = None,
        vault_writer: VaultNoteWriter | None = None,
    ) -> None:
        self._bus = bus
        self._matches = matches
        self._router = router
        self._vault_writer = vault_writer
        self._unsub: Any = None
        self._session_started_at: datetime | None = None

    def start(self) -> None:
        self._unsub = self._bus.subscribe(E.SYSTEM_MODE_CHANGED, self._on_mode_changed)

    async def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def _on_mode_changed(self, ev: Event) -> None:
        current = str(ev.payload.get("current", ""))
        previous = str(ev.payload.get("previous", ""))
        if current == "rocket_league" and self._session_started_at is None:
            self._session_started_at = datetime.now(UTC)
            return
        if previous == "rocket_league" and current != "rocket_league":
            started_at = self._session_started_at
            self._session_started_at = None
            if started_at is not None:
                await self._summarize_session(started_at)

    async def _summarize_session(self, started_at: datetime) -> None:
        matches = [m for m in self._matches.list_since(started_at) if m.ended_at is not None]
        if not matches:
            return  # nothing to summarize (Spec §3.3/ST-12-07 AC)
        wins = sum(1 for m in matches if m.result == "win")
        losses = sum(1 for m in matches if m.result == "loss")
        text = self._template_summary(matches, wins, losses)
        if self._router is not None:
            with contextlib.suppress(Exception):
                text = await self._ai_summary(matches, wins, losses) or text
        for match in matches:
            self._matches.set_detailed_summary(match.id, text)
        if self._vault_writer is not None:
            with contextlib.suppress(Exception):
                self._vault_writer.write(
                    session_started_at=started_at, text=text, match_ids=[m.id for m in matches]
                )

    def _template_summary(self, matches: list[Any], wins: int, losses: int) -> str:
        n = len(matches)
        record = f"{wins}W-{losses}L" if wins or losses else "result unknown"
        return (
            f"{n} match{'es' if n != 1 else ''} played ({record}). "
            "Deeper pattern analysis (recurring mistakes, MMR trend) is not yet available in this "
            "release - only match facts and recorded events are reported (Spec v0.3 §2.2/§15)."
        )

    async def _ai_summary(self, matches: list[Any], wins: int, losses: int) -> str | None:
        from nox.ai.base import AiRequest, AiRole, Message

        assert self._router is not None  # only called when set (see _summarize_session)
        facts = "\n".join(
            f"- match {m.id}: {m.result}, {m.score_self}-{m.score_opponent}" for m in matches
        )
        request = AiRequest(
            request_id=uuid.uuid4().hex,
            role=AiRole.BACKGROUND,
            mode="rocket_league",
            stream=False,
            messages=[
                Message(
                    role="system",
                    content=(
                        "Summarize this Rocket League session in 2-3 factual sentences (German). "
                        "Only use the facts given; never invent pattern insights you have no data "
                        "for; say plainly that deeper pattern analysis is not yet available."
                    ),
                ),
                Message(
                    role="user", content=f"{wins}W-{losses}L over {len(matches)} matches.\n{facts}"
                ),
            ],
        )
        response = await self._router.complete(request)
        return str(getattr(response, "text", "") or "") or None

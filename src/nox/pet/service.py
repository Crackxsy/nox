"""PetService: maps system events to the pet's functional state, mood and expression and publishes
`pet.state_changed` (Component Model, PRD FR-8.x). Pure event logic; rendering happens in ui/pet."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from nox.core.events import E, Event, EventBus, PetStateChanged
from nox.core.logging import get_logger
from nox.core.state import EXPRESSIONS, Mood, PetFunctional, StateManager

log = get_logger(__name__)

# Baseline the mood decays towards when nothing happens (config `pet.mood_baseline` may override).
DEFAULT_BASELINE = Mood()
DECAY_PER_TICK = 0.02


def expression_for(functional: PetFunctional, mood: Mood, mode: str) -> tuple[str, float]:
    """Deterministic mapping functional state x mood x mode -> (expression, intensity)."""
    if functional is PetFunctional.UNAVAILABLE:
        return "sleeping", 0.3
    if functional is PetFunctional.ERROR:
        return "confused", 0.7
    if functional is PetFunctional.PRIVACY:
        return "shy", 0.5
    if functional is PetFunctional.MUTED:
        return "bored", 0.4
    if functional is PetFunctional.THINKING:
        return "thinking", 0.6
    if functional is PetFunctional.WORKING:
        return "coding" if mode == "coding" else "working", 0.6
    if functional is PetFunctional.SPEAKING:
        return ("excited", 0.8) if mood.mood > 0.75 else ("happy", 0.6)
    if functional is PetFunctional.LISTENING:
        return "curious", 0.6
    # idle: mood driven
    if mode == "stream":
        return "streaming", 0.5
    if mode == "rocket_league":
        return "coaching", 0.5
    if mood.energy < 0.25:
        return "sleeping", 0.4
    if mood.stress > 0.7:
        return "tilted", min(1.0, mood.stress)
    if mood.mood > 0.8:
        return "happy", 0.6
    if mood.mood < 0.3:
        return "sad", 0.5
    if mood.curiosity > 0.75:
        return "curious", 0.5
    return "normal", 0.5


class PetService:
    def __init__(
        self,
        bus: EventBus,
        state: StateManager,
        *,
        baseline: Mood | None = None,
        tick_seconds: float = 30.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._bus = bus
        self._state = state
        self._baseline = baseline or DEFAULT_BASELINE
        self._tick = tick_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._functional = PetFunctional.IDLE
        self._mood = Mood()
        self._unsubs: list[Callable[[], None]] = []
        self._task: asyncio.Task[None] | None = None
        self._thinking = False
        self._speaking = False
        self._muted = False
        self._safe_mode = False
        self._privacy_zone = False

    # -- lifecycle ---------------------------------------------------------------------------------
    async def start(self) -> None:
        sub = self._bus.subscribe
        self._unsubs = [
            sub(E.VOICE_INPUT_STARTED, self._on_input_started),
            sub(E.VOICE_INPUT_STOPPED, self._on_input_stopped),
            sub(E.VOICE_MUTED, self._on_muted),
            sub(E.AI_REQUEST_STARTED, self._on_ai_started),
            sub(E.AI_RESPONSE_READY, self._on_ai_ready),
            sub(E.AI_REQUEST_FAILED, self._on_ai_failed),
            sub(E.TTS_STARTED, self._on_tts_started),
            sub(E.TTS_FINISHED, self._on_tts_done),
            sub(E.TTS_INTERRUPTED, self._on_tts_done),
            sub(E.SECURITY_KILL_SWITCH, self._on_kill),
            sub(E.SYSTEM_STARTED, self._on_system_started),
            sub(E.SYSTEM_HEALTH_CHANGED, self._on_health),
            sub(E.PET_INTERACTION, self._on_interaction),
            sub("privacy.zone_changed", self._on_zone),
        ]
        self._task = asyncio.create_task(self._mood_loop(), name="pet-mood")
        await self._emit(reason="start")

    async def stop(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()
        if self._task:
            self._task.cancel()
            self._task = None

    # -- public -----------------------------------------------------------------------------------
    @property
    def functional(self) -> PetFunctional:
        return self._functional

    @property
    def mood(self) -> Mood:
        return self._mood

    def snapshot(self) -> PetStateChanged:
        expression, intensity = expression_for(self._functional, self._mood, self._mode())
        return PetStateChanged(
            functional=self._functional.value,
            mood=self._mood.model_dump(),
            expression=expression,
            intensity=intensity,
        )

    async def set_functional(self, functional: PetFunctional, *, reason: str = "") -> None:
        if functional is self._functional:
            return
        self._functional = functional
        await self._emit(reason=reason)

    # -- handlers ----------------------------------------------------------------------------------
    async def _on_input_started(self, _: Event) -> None:
        if self._blocked():
            return
        await self.set_functional(PetFunctional.LISTENING, reason="voice.input_started")

    async def _on_input_stopped(self, _: Event) -> None:
        if self._blocked():
            return
        if not self._thinking and not self._speaking:
            await self.set_functional(PetFunctional.IDLE, reason="voice.input_stopped")

    async def _on_muted(self, ev: Event) -> None:
        self._muted = bool(ev.payload.get("muted", False))
        await self._recompute("voice.muted")

    async def _on_ai_started(self, _: Event) -> None:
        self._thinking = True
        if not self._blocked():
            await self.set_functional(PetFunctional.THINKING, reason="ai.request_started")

    async def _on_ai_ready(self, _: Event) -> None:
        self._thinking = False
        self._nudge(mood=+0.03, curiosity=+0.02)
        if not self._speaking:
            await self._recompute("ai.response_ready")

    async def _on_ai_failed(self, ev: Event) -> None:
        self._thinking = False
        if ev.payload.get("fallback_to"):
            return  # router continues with a fallback provider
        self._nudge(stress=+0.05, mood=-0.03)
        if not self._blocked():
            await self.set_functional(PetFunctional.ERROR, reason="ai.request_failed")
            asyncio.get_running_loop().call_later(
                3.0, lambda: asyncio.create_task(self._recompute("error.timeout"))
            )

    async def _on_tts_started(self, _: Event) -> None:
        self._speaking = True
        if not self._blocked():
            await self.set_functional(PetFunctional.SPEAKING, reason="tts.started")

    async def _on_tts_done(self, _: Event) -> None:
        self._speaking = False
        await self._recompute("tts.done")

    async def _on_kill(self, _: Event) -> None:
        self._safe_mode = True
        self._thinking = self._speaking = False
        await self.set_functional(PetFunctional.UNAVAILABLE, reason="security.kill_switch")

    async def _on_system_started(self, _: Event) -> None:
        self._safe_mode = False
        await self._recompute("system.started")

    async def _on_health(self, ev: Event) -> None:
        if ev.payload.get("component") == "core" and ev.payload.get("status") == "unavailable":
            await self.set_functional(PetFunctional.UNAVAILABLE, reason="health")

    async def _on_interaction(self, ev: Event) -> None:
        kind = ev.payload.get("type", "click")
        if kind == "click":
            self._nudge(affection=+0.05, attention=+0.1, mood=+0.02)
        elif kind == "drag":
            self._nudge(stress=+0.03, attention=+0.1)
        await self._emit(reason=f"pet.interaction:{kind}")

    async def _on_zone(self, ev: Event) -> None:
        self._privacy_zone = bool(ev.payload.get("active", False))
        await self._recompute("privacy.zone_changed")

    # -- internals ---------------------------------------------------------------------------------
    def _blocked(self) -> bool:
        return self._safe_mode or self._muted or self._privacy_zone

    def _mode(self) -> str:
        try:
            return str(self._state.get("assistant.mode"))
        except Exception:  # state may not be ready during tests
            return "companion"

    async def _recompute(self, reason: str) -> None:
        if self._safe_mode:
            target = PetFunctional.UNAVAILABLE
        elif self._privacy_zone:
            target = PetFunctional.PRIVACY
        elif self._muted:
            target = PetFunctional.MUTED
        elif self._speaking:
            target = PetFunctional.SPEAKING
        elif self._thinking:
            target = PetFunctional.THINKING
        else:
            target = PetFunctional.IDLE
        await self.set_functional(target, reason=reason)

    def _nudge(self, **deltas: float) -> None:
        data: dict[str, Any] = self._mood.model_dump()
        for key, delta in deltas.items():
            data[key] = max(0.0, min(1.0, data[key] + delta))
        self._mood = Mood(**data)

    async def _mood_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._tick)
                self.decay_once()
                await self._emit(reason="mood.tick")
        except asyncio.CancelledError:
            pass

    def decay_once(self) -> None:
        data = self._mood.model_dump()
        base = self._baseline.model_dump()
        for key, value in data.items():
            target = base[key]
            if value > target:
                data[key] = max(target, value - DECAY_PER_TICK)
            elif value < target:
                data[key] = min(target, value + DECAY_PER_TICK)
        self._mood = Mood(**data)

    async def _emit(self, *, reason: str) -> None:
        snap = self.snapshot()
        assert snap.expression in EXPRESSIONS
        try:
            await self._state.update("assistant.pet_functional", snap.functional, reason=reason)
            await self._state.update("assistant.expression", snap.expression, reason=reason)
            await self._state.update(
                "assistant.expression_intensity", snap.intensity, reason=reason
            )
            await self._state.update("assistant.mood", snap.mood, reason=reason)
        except Exception as exc:  # state manager failures must not break the pet
            log.warning("pet.state_update_failed", error=str(exc))
        await self._bus.publish(
            Event(name=E.PET_STATE_CHANGED, payload=snap.model_dump(), source="pet")
        )

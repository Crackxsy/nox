"""Orchestrator: turns a user utterance/text into an AI request, streams the answer sentence-wise to
the voice layer and records the turn (Component Model §Orchestrator flow, v0.1 Walking Skeleton).
Every side effect stays behind the injected callables; the orchestrator never touches hardware."""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from nox.ai.base import AiRequest, AiRole, Message, Router
from nox.core.events import E, Event, EventBus
from nox.core.logging import get_logger
from nox.core.state import StateManager
from nox.voice.base import Channel, TtsRequest

log = get_logger(__name__)

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|(?<=[.!?…])$")


class Speaker(Protocol):
    async def say(self, request: TtsRequest) -> None: ...
    async def interrupt(self, *, reason: str) -> None: ...


class TurnStore(Protocol):
    async def record(
        self, session_id: str, role: str, text: str, *, provider: str = "", latency_ms: int = 0
    ) -> None: ...
    async def recent(self, session_id: str, limit: int) -> list[tuple[str, str]]: ...


class MemoryPolicy(Protocol):
    def allows_memory_write(self) -> bool: ...


@dataclass
class Turn:
    request_id: str
    text: str
    response: str = ""
    provider: str = ""
    degraded: bool = False
    chunks: int = 0


@dataclass
class OrchestratorConfig:
    history_turns: int = 8
    max_tokens: int = 400
    default_language: str = "de"
    channel: Channel = Channel.PRIVATE
    speak: bool = True


@dataclass
class Orchestrator:
    bus: EventBus
    state: StateManager
    router: Router
    speaker: Speaker | None
    turns: TurnStore | None
    memory_policy: MemoryPolicy | None
    system_prompt: Callable[[], str]
    config: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    _active: asyncio.Task[Turn] | None = None
    voice_turn: asyncio.Task[None] | None = None
    _unsubs: list[Callable[[], None]] = field(default_factory=list)
    _safe_mode: bool = False

    async def start(self) -> None:
        self._unsubs = [
            self.bus.subscribe(E.VOICE_TRANSCRIPT_READY, self._on_transcript),
            self.bus.subscribe(E.SECURITY_KILL_SWITCH, self._on_kill),
            self.bus.subscribe(E.SYSTEM_STARTED, self._on_started),
            self.bus.subscribe(E.SYSTEM_STOPPING, self._on_stopping),
        ]
        await self.bus.publish(
            Event(name=E.SESSION_STARTED, payload={"session_id": self.session_id})
        )

    async def stop(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()
        await self.cancel(reason="stop")
        if self.voice_turn is not None and not self.voice_turn.done():
            self.voice_turn.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.voice_turn
        await self.bus.publish(Event(name=E.SESSION_ENDED, payload={"session_id": self.session_id}))

    # -- entry points -----------------------------------------------------------------------------
    async def handle_text(
        self,
        text: str,
        *,
        language: str | None = None,
        speak: bool | None = None,
        on_chunk: Callable[[str], Awaitable[None] | None] | None = None,
    ) -> Turn:
        """Process a user utterance. Cancels an in-flight turn (barge-in semantics)."""
        if self._safe_mode:
            raise RuntimeError("safe mode: AI requests are disabled until resume")
        await self.cancel(reason="new_input")
        self._active = asyncio.create_task(
            self._run(text, language or self.config.default_language, speak, on_chunk)
        )
        try:
            return await self._active
        finally:
            self._active = None

    async def cancel(self, *, reason: str) -> None:
        task = self._active
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception) as exc:
                log.debug("orchestrator.cancelled", reason=reason, error=str(exc))
        if self.speaker is not None:
            try:
                await self.speaker.interrupt(reason=reason)
            except Exception as exc:
                log.warning("orchestrator.interrupt_failed", error=str(exc))

    # -- handlers ----------------------------------------------------------------------------------
    async def _on_transcript(self, ev: Event) -> None:
        if not ev.payload.get("addressed_to_nox", False):
            return
        text = str(ev.payload.get("text", "")).strip()
        if not text:
            return
        # Never await the turn here. This handler runs inside the hub's delivery of the worker's
        # inbound event; awaiting LLM + TTS there stalled that connection's dispatch, and the
        # `tts.finished` the turn waits for (from the same worker) could never arrive - a deadlock
        # that killed the worker after 45 s of missed heartbeats (2026-09-15).
        language = str(ev.payload.get("language") or self.config.default_language)
        self.voice_turn = asyncio.create_task(
            self._voice_turn(text, language), name="nox-voice-turn"
        )

    async def _voice_turn(self, text: str, language: str) -> None:
        try:
            await self.handle_text(text, language=language)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed turn is logged, never fatal
            log.warning("orchestrator.turn_failed", error=str(exc))

    async def _on_kill(self, _: Event) -> None:
        self._safe_mode = True
        await self.cancel(reason="kill_switch")

    async def _on_started(self, _: Event) -> None:
        self._safe_mode = False

    async def _on_stopping(self, _: Event) -> None:
        await self.cancel(reason="stopping")

    # -- core flow ---------------------------------------------------------------------------------
    async def _run(
        self,
        text: str,
        language: str,
        speak: bool | None,
        on_chunk: Callable[[str], Awaitable[None] | None] | None = None,
    ) -> Turn:
        request_id = uuid.uuid4().hex
        turn = Turn(request_id=request_id, text=text)
        messages = [Message(role="system", content=self.system_prompt())]
        if self.turns is not None:
            for role, content in await self.turns.recent(
                self.session_id, self.config.history_turns
            ):
                messages.append(Message(role=role, content=content))
        messages.append(Message(role="user", content=text))
        request = AiRequest(
            request_id=request_id,
            role=AiRole.CHAT,
            messages=messages,
            mode=str(self.state.get("assistant.mode")),
            privacy_mode=str(self.state.get("privacy.mode")),
            max_tokens=self.config.max_tokens,
            metadata={"language": language, "session_id": self.session_id},
        )
        await self._record("user", text)
        buffer = ""
        speak_enabled = self.config.speak if speak is None else speak
        ready: dict[str, object] = {}

        def _capture_ready(ev: Event) -> None:
            if ev.payload.get("request_id") == request_id:
                ready.update(ev.payload)

        unsub = self.bus.subscribe(E.AI_RESPONSE_READY, _capture_ready)
        try:
            async for chunk in self.router.stream(request):
                turn.chunks += 1
                buffer += chunk.delta
                turn.response += chunk.delta
                if on_chunk is not None and chunk.delta:
                    result = on_chunk(chunk.delta)
                    if result is not None:
                        await result
                if speak_enabled and self.speaker is not None:
                    sentences, buffer = self._split(buffer, final=chunk.done)
                    for sentence in sentences:
                        await self._speak(sentence, language, request_id)
            if speak_enabled and self.speaker is not None and buffer.strip():
                await self._speak(buffer.strip(), language, request_id)
            if not ready:  # router emitted ready after the generator finished: ask it
                explain = self.router.explain(request_id)
                ready = {
                    "provider": explain.split(" ")[0] if explain else "",
                    "degraded": "degraded" in explain,
                }
            turn.provider = str(ready.get("provider", ""))
            turn.degraded = bool(ready.get("degraded", False))
            await self._record("assistant", turn.response, provider=turn.provider)
            return turn
        finally:
            unsub()

    async def _speak(self, sentence: str, language: str, request_id: str) -> None:
        assert self.speaker is not None
        await self.speaker.say(
            TtsRequest(
                utterance_id=f"{request_id}:{uuid.uuid4().hex[:8]}",
                text=sentence,
                language=language,
                channel=self.config.channel,
            )
        )

    async def _record(self, role: str, text: str, *, provider: str = "") -> None:
        if self.turns is None:
            return
        if self.memory_policy is not None and not self.memory_policy.allows_memory_write():
            return
        try:
            await self.turns.record(self.session_id, role, text, provider=provider)
        except Exception as exc:
            log.warning("orchestrator.record_failed", error=str(exc))

    @staticmethod
    def _split(buffer: str, *, final: bool) -> tuple[list[str], str]:
        parts = [p for p in _SENTENCE_END.split(buffer) if p is not None]
        if final:
            return [p.strip() for p in parts if p.strip()], ""
        if len(parts) <= 1:
            return [], buffer
        return [p.strip() for p in parts[:-1] if p.strip()], parts[-1]

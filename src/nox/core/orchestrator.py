"""Orchestrator: turns a user utterance/text into an AI request, streams the answer sentence-wise to
the voice layer and records the turn (Component Model §Orchestrator flow, v0.1 Walking Skeleton).
Every side effect stays behind the injected callables; the orchestrator never touches hardware.

Every turn is timed per stage (:class:`TurnTimings`) because the felt latency of the assistant is
the product this module owns; `scripts/bench_chat.py` reads those numbers rather than guessing
them from the outside.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from nox.ai.base import AiRequest, AiRole, Message, Router
from nox.ai.escalation import EscalationPolicy
from nox.ai.fastpath import FastPath
from nox.ai.tooluse import decided_prose
from nox.core.events import E, Event, EventBus
from nox.core.logging import get_logger
from nox.core.state import StateManager
from nox.core.toolloop import ToolGate
from nox.voice.base import Channel, TtsRequest

log = get_logger(__name__)

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|(?<=[.!?…])$")

#: Said when the model kept asking for tools until the turn ran out of rounds. Deliberately
#: short: the user only needs to know that nothing happened, and the detail belongs in the log.
GAVE_UP_ON_TOOLS = {"de": "Das hat nicht funktioniert.", "en": "That did not work."}

#: Provider id reported for a turn the deterministic fast path answered. It is a real provider name
#: in the dashboard badge on purpose: the user must be able to see that no language model was asked.
FAST_PATH_PROVIDER = "fastpath"


class Speaker(Protocol):
    async def say(self, request: TtsRequest) -> None: ...
    async def interrupt(self, *, reason: str) -> None: ...


class PresetGate(Protocol):
    """Resolves a sentence to a preset, runs it, and returns what to answer.

    `None` means no preset matched and the sentence continues on its ordinary path.
    """

    async def handle(self, text: str, language: str) -> str | None: ...


class TurnStore(Protocol):
    async def record(
        self, session_id: str, role: str, text: str, *, provider: str = "", latency_ms: int = 0
    ) -> None: ...
    async def recent(self, session_id: str, limit: int) -> list[tuple[str, str]]: ...


class MemoryPolicy(Protocol):
    def allows_memory_write(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class TurnContext:
    """What the memory layer contributes to one turn.

    `block` is prompt-ready text (heading included) that is appended to the system message, or
    empty when nothing relevant was found. `relevance` is the best semantic score behind that
    block (0.0 when the block is empty or came from keyword search only), which is what the
    escalation policy weighs - a long question with no relevant memory is the case a 3B model
    answers badly.
    """

    block: str = ""
    relevance: float = 0.0
    items: int = 0


#: Awaited once per turn, before the system message is built. Returning an empty
#: :class:`TurnContext` means "nothing to add"; raising is the caller's bug, not the turn's.
ContextProvider = Callable[[str], Awaitable[TurnContext]]


@dataclass
class TurnTimings:
    """Wall-clock milliseconds per stage of one turn, measured, never estimated."""

    context_ms: float = 0.0
    prompt_build_ms: float = 0.0
    first_token_ms: float = 0.0
    #: First *complete sentence*, which is when sentence-wise TTS can start speaking.
    first_sentence_ms: float = 0.0
    total_ms: float = 0.0


@dataclass
class Turn:
    request_id: str
    text: str
    response: str = ""
    provider: str = ""
    degraded: bool = False
    chunks: int = 0
    timings: TurnTimings = field(default_factory=TurnTimings)
    #: Fast-path intent that answered this turn without a language model; empty when a model did.
    fast_path: str = ""
    #: Why this turn was routed to the reasoning chain instead of the chat chain; empty when not.
    escalated: str = ""
    #: Tools this turn used, in order. Empty for the great majority of turns, and that is the
    #: point: it is data, so "did it actually do something" is answerable without the transcript.
    tools_used: list[str] = field(default_factory=list)


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
    #: Set by `nox.memory.install` to make the prompt retrieval-augmented. `None` = no memory.
    context_provider: ContextProvider | None = None
    #: Set by `nox.presets.install` so a registered phrase reaches its preset before any
    #: model is asked. `None` = no presets.
    preset_gate: PresetGate | None = None
    #: Set by `nox.capabilities.install` so that everything reachable from the dashboard is
    #: also reachable by saying it. `None` = the model can only talk, which was the state
    #: until now.
    tool_gate: ToolGate | None = None
    fast_path: FastPath = field(default_factory=FastPath)
    escalation: EscalationPolicy = field(default_factory=EscalationPolicy)

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
        started = time.perf_counter()
        request_id = uuid.uuid4().hex
        turn = Turn(request_id=request_id, text=text)
        speak_enabled = self.config.speak if speak is None else speak

        if self.preset_gate is not None:
            spoken = await self.preset_gate.handle(text, language)
            if spoken is not None:
                turn.fast_path = "preset"
                return await self._deliver_fast_path(
                    turn,
                    spoken,
                    language,
                    speak=speak_enabled,
                    on_chunk=on_chunk,
                    started=started,
                )

        answer = self.fast_path.match(text, language)
        if answer is not None:
            turn.fast_path = answer.intent
            return await self._deliver_fast_path(
                turn, answer.text, language, speak=speak_enabled, on_chunk=on_chunk, started=started
            )

        context = await self._retrieve_context(text, turn.timings)
        build_started = time.perf_counter()
        tools = self.tool_gate.prompt() if self.tool_gate is not None else ""
        messages = [Message(role="system", content=self._prompt_with(context, tools))]
        if self.turns is not None:
            for role, content in await self.turns.recent(
                self.session_id, self.config.history_turns
            ):
                messages.append(Message(role=role, content=content))
        messages.append(Message(role="user", content=text))
        decision = self.escalation.decide(text, relevance=context.relevance)
        turn.escalated = decision.reason
        turn.timings.prompt_build_ms = (time.perf_counter() - build_started) * 1000
        if decision.reason:
            log.info("orchestrator.escalated", request_id=request_id, reason=decision.reason)
        await self._record("user", text)
        ready: dict[str, object] = {}

        def _capture_ready(ev: Event) -> None:
            if ev.payload.get("request_id") == request_id:
                ready.update(ev.payload)

        unsub = self.bus.subscribe(E.AI_RESPONSE_READY, _capture_ready)
        rounds = self.tool_gate.max_rounds if self.tool_gate is not None and tools else 0
        try:
            for attempt in range(rounds + 1):
                final_attempt = attempt == rounds
                if final_attempt and rounds:
                    # Out of tool rounds. Take the offer away so the model has to answer with what
                    # it has; the alternative is a user left with silence or with a protocol line.
                    messages[0] = Message(role="system", content=self._prompt_with(context))
                    log.info("orchestrator.tool_rounds_spent", request_id=request_id)
                produced, directive = await self._stream(
                    self._request(request_id, decision.role, messages, language),
                    turn,
                    language,
                    speak=speak_enabled,
                    on_chunk=on_chunk,
                    started=started,
                    # Held on every attempt, the last one included: a model that asks for a tool
                    # after the offer was withdrawn must still never be read aloud.
                    hold=rounds > 0,
                )
                if not directive or self.tool_gate is None:
                    break
                if final_attempt:
                    log.warning("orchestrator.directive_without_an_offer", request_id=request_id)
                    await self._deliver_plain(
                        turn,
                        GAVE_UP_ON_TOOLS.get(language[:2], GAVE_UP_ON_TOOLS["en"]),
                        language,
                        speak=speak_enabled,
                        on_chunk=on_chunk,
                    )
                    break
                used = await self.tool_gate.advance(produced)
                if used is None:  # the sniffer was wrong about the head; nothing to hand back
                    break
                if used.tool:
                    turn.tools_used.append(used.tool)
                messages.append(Message(role="assistant", content=produced))
                messages.append(Message(role="user", content=used.feedback))
            if not ready:  # router emitted ready after the generator finished: ask it
                explain = self.router.explain(request_id)
                ready = {
                    "provider": explain.split(" ")[0] if explain else "",
                    "degraded": "degraded" in explain,
                }
            turn.provider = str(ready.get("provider", ""))
            turn.degraded = bool(ready.get("degraded", False))
            self._note_first_sentence(turn, started)
            turn.timings.total_ms = (time.perf_counter() - started) * 1000
            await self._record("assistant", turn.response, provider=turn.provider)
            return turn
        finally:
            unsub()

    def _request(
        self, request_id: str, role: AiRole, messages: list[Message], language: str
    ) -> AiRequest:
        """One model request. Built per round, because the messages grow with every tool result."""
        return AiRequest(
            request_id=request_id,
            role=role,
            messages=list(messages),
            mode=str(self.state.get("assistant.mode")),
            privacy_mode=str(self.state.get("privacy.mode")),
            max_tokens=self.config.max_tokens,
            metadata={"language": language, "session_id": self.session_id},
        )

    async def _deliver_plain(
        self,
        turn: Turn,
        text: str,
        language: str,
        *,
        speak: bool,
        on_chunk: Callable[[str], Awaitable[None] | None] | None,
    ) -> None:
        """Put one sentence of Nox's own on the same path a streamed answer takes.

        Used only when the model produced nothing usable. Kept separate from `_deliver_fast_path`
        because that one owns a whole turn, including recording the user message.
        """
        turn.response = text
        if on_chunk is not None:
            result = on_chunk(text)
            if result is not None:
                await result
        if speak and self.speaker is not None:
            await self._speak(text, language, turn.request_id)

    async def _stream(
        self,
        request: AiRequest,
        turn: Turn,
        language: str,
        *,
        speak: bool,
        on_chunk: Callable[[str], Awaitable[None] | None] | None,
        started: float,
        hold: bool,
    ) -> tuple[str, bool]:
        """Stream one model answer; returns the whole text and whether it was a tool directive.

        `hold` is what keeps the user from hearing `NOX_TOOL_CALL` read aloud. While the first
        characters could still turn into the sentinel, nothing is emitted, spoken or added to the
        turn; once it is clear this is prose, the held text is released in one piece and the rest
        streams as before. The cost is the latency of about a dozen characters, paid only in turns
        where tools are on offer at all.
        """
        produced = ""
        pending = ""
        buffer = ""
        directive = False
        decided = not hold

        async for chunk in self.router.stream(request):
            turn.chunks += 1
            produced += chunk.delta
            if chunk.delta and turn.timings.first_token_ms == 0.0:
                turn.timings.first_token_ms = (time.perf_counter() - started) * 1000
            release = chunk.delta
            if not decided:
                pending += chunk.delta
                verdict = decided_prose(pending)
                if verdict is None and not chunk.done:
                    continue
                decided = True
                directive = verdict is False
                release, pending = ("" if directive else pending), ""
            if directive:
                continue
            turn.response += release
            buffer += release
            if on_chunk is not None and release:
                result = on_chunk(release)
                if result is not None:
                    await result
            if speak and self.speaker is not None:
                sentences, buffer = self._split(buffer, final=chunk.done)
                for sentence in sentences:
                    self._note_first_sentence(turn, started)
                    await self._speak(sentence, language, turn.request_id)
            elif turn.timings.first_sentence_ms == 0.0 and _SENTENCE_END.search(buffer):
                # Not speaking, but the number still matters: it is when speech *could* start.
                self._note_first_sentence(turn, started)
        if not directive and speak and self.speaker is not None and buffer.strip():
            self._note_first_sentence(turn, started)
            await self._speak(buffer.strip(), language, turn.request_id)
        return produced, directive

    async def _retrieve_context(self, text: str, timings: TurnTimings) -> TurnContext:
        """Memory context for this turn. A failing provider degrades to no context, not a
        failed turn."""
        if self.context_provider is None:
            return TurnContext()
        started = time.perf_counter()
        try:
            return await self.context_provider(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a broken retrieval must not break the turn
            log.warning("orchestrator.context_failed", error=f"{type(exc).__name__}: {exc}")
            return TurnContext()
        finally:
            timings.context_ms = (time.perf_counter() - started) * 1000

    def _prompt_with(self, context: TurnContext, tools: str = "") -> str:
        """System message for one turn: personality, then memory, then the tools on offer.

        The tool section goes last so that the names the model may use are the freshest thing
        in the prompt, and it is left out entirely when nothing is on offer - a model told
        about a protocol it cannot use will eventually try to use it anyway.
        """
        parts = [self.system_prompt(), context.block, tools]
        return "\n\n".join(part for part in parts if part)

    @staticmethod
    def _note_first_sentence(turn: Turn, started: float) -> None:
        if turn.timings.first_sentence_ms == 0.0 and turn.response.strip():
            turn.timings.first_sentence_ms = (time.perf_counter() - started) * 1000

    async def _deliver_fast_path(
        self,
        turn: Turn,
        text: str,
        language: str,
        *,
        speak: bool,
        on_chunk: Callable[[str], Awaitable[None] | None] | None,
        started: float,
    ) -> Turn:
        """Deliver a deterministic answer on the same path a streamed one takes.

        The consumers downstream (dashboard stream frames, TTS, turn history) must not be able to
        tell the difference apart from the provider badge, which says `fastpath` so the user can
        see that no language model was involved.
        """
        await self._record("user", turn.text)
        turn.response = text
        turn.chunks = 1
        turn.provider = FAST_PATH_PROVIDER
        turn.timings.first_token_ms = (time.perf_counter() - started) * 1000
        if on_chunk is not None:
            result = on_chunk(text)
            if result is not None:
                await result
        if speak and self.speaker is not None:
            sentences, _ = self._split(text, final=True)
            for sentence in sentences:
                await self._speak(sentence, language, turn.request_id)
        turn.timings.first_sentence_ms = (time.perf_counter() - started) * 1000
        turn.timings.total_ms = (time.perf_counter() - started) * 1000
        await self._record("assistant", turn.response, provider=turn.provider)
        return turn

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

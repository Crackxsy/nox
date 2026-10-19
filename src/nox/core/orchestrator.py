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

from nox.ai.base import AiRequest, AiRole, Message, Router, ToolCallRequest, ToolDefinition
from nox.ai.escalation import EscalationPolicy
from nox.ai.fastpath import FastPath
from nox.ai.tooluse import render_result
from nox.core.events import E, Event, EventBus
from nox.core.logging import get_logger
from nox.core.state import StateManager
from nox.core.toolloop import ActiveTurn, ToolEvent, ToolLoop
from nox.voice.base import Channel, TtsRequest

log = get_logger(__name__)

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|(?<=[.!?…])$")

#: Clause boundaries the first utterance of a turn may be cut at, and the length from which a long
#: opening sentence is worth starting early: below ~40 characters the sentence ends soon anyway,
#: and a two-word fragment spoken alone sounds broken.
_CLAUSE_MARKS = (", ", ": ", " - ", " – ", "; ")
FIRST_CLAUSE_MIN_CHARS = 40

#: A turn with tools may take its whole budget plus time for dialogs and the final answer.
TOOL_TURN_MARGIN_S = 30.0

#: Said once when a tool loop keeps the user waiting with nothing said yet (`filler_delay_s`).
TOOL_FILLER = {"de": "Moment, ich schau nach.", "en": "One moment, let me check."}

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
    #: How long a tool loop may keep a speaking user waiting in silence before the filler.
    filler_delay_s: float = 2.0


@dataclass
class _TurnSpeech:
    """Speech state of one turn: whether anything was said yet, and the pending filler."""

    speak: bool
    spoken: bool = False
    filler_task: asyncio.Task[None] | None = None

    def text_arrived(self) -> None:
        """The model's own words are here: a filler that has not started yet is not needed."""
        task = self.filler_task
        if task is not None and not task.done() and not self.spoken:
            task.cancel()

    def cancel_filler(self) -> None:
        if self.filler_task is not None and not self.filler_task.done():
            self.filler_task.cancel()


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
    #: Set by `nox.memory.install`: what Nox knows about the user, rendered for the prompt. It sits
    #: between the stable personality and the per-turn memory context, so it is cached as part of
    #: the prefix until a fact changes. `None` = nothing known, or no memory.
    profile_block: Callable[[], str] | None = None
    #: Set by `nox.memory.install`: told about every finished model turn, so facts about the user
    #: can be captured from it in the background. Never awaited inside the turn.
    on_turn_complete: Callable[[str, str], None] | None = None
    #: Set by `nox.presets.install` so a registered phrase reaches its preset before any
    #: model is asked. `None` = no presets.
    preset_gate: PresetGate | None = None
    #: Set by `NoxCore` so "lass uns streamen" switches to stream mode instead of being talked
    #: about (`nox.core.mode_intents`). Asked after the presets: a phrase the user registered
    #: themselves wins over the built-in one. Same contract as `PresetGate`.
    mode_gate: PresetGate | None = None
    #: Set by `nox.capabilities.install` so that everything reachable from the dashboard is
    #: also reachable by saying it. `None` = the model can only talk.
    tool_loop: ToolLoop | None = None
    #: Current facts (time, mode, privacy) for the turn context; `None` = none.
    facts: Callable[[], str] | None = None
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
        if self.tool_loop is not None:
            await self.tool_loop.end_all()

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

        if self.mode_gate is not None:
            spoken = await self.mode_gate.handle(text, language)
            if spoken is not None:
                turn.fast_path = "mode"
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
        tools = self.tool_loop.begin(request_id) if self.tool_loop is not None else None
        messages = [Message(role="system", content=self.system_prompt())]
        if self.turns is not None:
            for role, content in await self.turns.recent(
                self.session_id, self.config.history_turns
            ):
                messages.append(Message(role=role, content=content))
        messages.append(Message(role="user", content=text))
        turn_context = self._turn_context(context, with_caveats=tools is not None)
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
        speech = _TurnSpeech(speak=speak_enabled and self.speaker is not None)
        if tools is not None:
            tools.listeners.append(lambda event: self._on_tool_event(event, speech, language, turn))
        try:
            await self._converse(
                turn,
                messages,
                decision.role,
                language,
                turn_context,
                tools,
                speech,
                on_chunk,
                started,
            )
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
            self._notify_turn_complete(text, turn.response)
            return turn
        finally:
            unsub()
            speech.cancel_filler()
            if tools is not None and self.tool_loop is not None:
                turn.tools_used = list(tools.used)
                await self.tool_loop.end(tools)

    async def _converse(
        self,
        turn: Turn,
        messages: list[Message],
        role: AiRole,
        language: str,
        turn_context: str,
        tools: ActiveTurn | None,
        speech: _TurnSpeech,
        on_chunk: Callable[[str], Awaitable[None] | None] | None,
        started: float,
    ) -> None:
        """Model steps until the model answers without asking for a tool.

        A provider that runs its own agent loop (Claude Code through the MCP bridge) finishes in
        one step: its tool calls reach `ToolLoop.execute` from the bridge. A provider Nox drives
        (Ollama, native or through the sentinel adapter) hands its calls back here; Nox runs them
        and asks again with the results. Either way the turn's `TurnBudget` decides how far it
        goes, and once it is spent the tools are withdrawn so the model has to answer.
        """
        offered = tools.definitions() if tools is not None else []
        max_steps = (tools.budget.max_calls + 2) if tools is not None else 1
        for _step in range(max_steps):
            request = self._request(
                turn.request_id,
                role,
                messages,
                language,
                context=turn_context,
                tools=offered,
                tool_turn=tools.token if tools is not None else "",
                timeout_s=(
                    tools.budget.wall_clock_s + TOOL_TURN_MARGIN_S if tools is not None else None
                ),
            )
            produced, calls = await self._stream(request, turn, language, speech, on_chunk, started)
            if not calls:
                return
            if tools is None or self.tool_loop is None:
                log.warning("orchestrator.tool_calls_without_an_offer", request_id=turn.request_id)
                break
            messages.append(Message(role="assistant", content=produced, tool_calls=calls))
            for call in calls:
                run = await self.tool_loop.execute(tools, call.name, call.arguments)
                messages.append(
                    Message(
                        role="tool",
                        content=render_result(call.name, ok=run.ok, data=run.data, error=run.error),
                        tool_name=call.name,
                        tool_call_id=call.id,
                    )
                )
            if tools.budget.exhausted:
                # Out of budget. Take the offer away so the model answers with what it has; the
                # alternative is a user left with silence or with a refusal loop.
                offered = []
                log.info("orchestrator.tool_budget_spent", request_id=turn.request_id)
        if not turn.response.strip():
            await self._deliver_plain(
                turn,
                GAVE_UP_ON_TOOLS.get(language[:2], GAVE_UP_ON_TOOLS["en"]),
                language,
                speak=speech.speak,
                on_chunk=on_chunk,
            )

    def _notify_turn_complete(self, user_text: str, response: str) -> None:
        if self.on_turn_complete is None or not response.strip():
            return
        if self.memory_policy is not None and not self.memory_policy.allows_memory_write():
            return
        try:
            self.on_turn_complete(user_text, response)
        except Exception as exc:  # noqa: BLE001 - a listener must never break the turn it heard
            log.warning("orchestrator.turn_listener_failed", error=f"{type(exc).__name__}: {exc}")

    def _request(
        self,
        request_id: str,
        role: AiRole,
        messages: list[Message],
        language: str,
        *,
        context: str = "",
        tools: list[ToolDefinition] | None = None,
        tool_turn: str = "",
        timeout_s: float | None = None,
    ) -> AiRequest:
        """One model request. Built per step, because the messages grow with every tool result."""
        return AiRequest(
            request_id=request_id,
            role=role,
            messages=list(messages),
            mode=str(self.state.get("assistant.mode")),
            privacy_mode=str(self.state.get("privacy.mode")),
            max_tokens=self.config.max_tokens,
            metadata={"language": language, "session_id": self.session_id},
            context=context,
            tools=list(tools or []),
            tool_turn=tool_turn,
            **({"timeout_s": timeout_s} if timeout_s is not None else {}),
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
        speech: _TurnSpeech,
        on_chunk: Callable[[str], Awaitable[None] | None] | None,
        started: float,
    ) -> tuple[str, list[ToolCallRequest]]:
        """Stream one model step; returns its text and the tool calls it asked Nox to run.

        Text is released as it arrives - there is no protocol line to hide any more: native tool
        calls arrive as data, and the sentinel fallback is parsed inside the provider. Speech
        starts at the first complete sentence (or, for a long opening sentence, its first clause).
        """
        produced = ""
        buffer = ""
        calls: list[ToolCallRequest] = []
        async for chunk in self.router.stream(request):
            turn.chunks += 1
            if chunk.tool_activity:
                self._on_provider_tools(chunk.tool_activity, speech, language, turn)
            calls.extend(chunk.tool_calls)
            if not chunk.delta and not chunk.done:
                continue
            if chunk.delta and turn.timings.first_token_ms == 0.0:
                turn.timings.first_token_ms = (time.perf_counter() - started) * 1000
            produced += chunk.delta
            turn.response += chunk.delta
            buffer += chunk.delta
            if chunk.delta:
                speech.text_arrived()
            if on_chunk is not None and chunk.delta:
                result = on_chunk(chunk.delta)
                if result is not None:
                    await result
            if speech.speak:
                sentences, buffer = self._split(buffer, final=chunk.done, first=not speech.spoken)
                for sentence in sentences:
                    self._note_first_sentence(turn, started)
                    speech.spoken = True
                    await self._speak(sentence, language, turn.request_id)
            elif turn.timings.first_sentence_ms == 0.0 and _SENTENCE_END.search(buffer):
                # Not speaking, but the number still matters: it is when speech *could* start.
                self._note_first_sentence(turn, started)
        if speech.speak and buffer.strip():
            self._note_first_sentence(turn, started)
            speech.spoken = True
            await self._speak(buffer.strip(), language, turn.request_id)
        return produced, calls

    # -- tool activity and the "let me check" filler -------------------------------------------

    def _on_tool_event(
        self, event: ToolEvent, speech: _TurnSpeech, language: str, turn: Turn
    ) -> None:
        if event.kind == "started":
            self._arm_filler(speech, language, turn)

    def _on_provider_tools(
        self, names: list[str], speech: _TurnSpeech, language: str, turn: Turn
    ) -> None:
        log.debug("orchestrator.provider_tools", request_id=turn.request_id, tools=names)
        self._arm_filler(speech, language, turn)

    def _arm_filler(self, speech: _TurnSpeech, language: str, turn: Turn) -> None:
        """Say "let me check" if a tool loop keeps the user waiting with nothing said yet.

        Once per turn, only when speaking, only if nothing has been said so far, and called off
        the moment the model's own words arrive - a model that already said "ich schau nach"
        must not be followed by Nox saying it again.
        """
        if not speech.speak or speech.spoken or speech.filler_task is not None:
            return

        async def _filler() -> None:
            await asyncio.sleep(self.config.filler_delay_s)
            if speech.spoken:
                return
            speech.spoken = True
            text = TOOL_FILLER.get(language[:2], TOOL_FILLER["en"])
            log.info("orchestrator.filler", request_id=turn.request_id)
            await self._speak(text, language, turn.request_id)

        speech.filler_task = asyncio.create_task(_filler(), name="nox-tool-filler")

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

    def _turn_context(self, context: TurnContext, *, with_caveats: bool) -> str:
        """Per-turn context: current facts, what Nox knows about the user, memory, and what is
        not available right now (only when tools are on offer - a model told about caveats of a
        protocol it cannot use will eventually try to use it anyway).

        Kept out of the system message so the stable prefix stays cacheable and a provider with a
        long-lived session can send it along with the user's message.
        """
        facts = self._facts()
        caveats = self.tool_loop.caveats() if with_caveats and self.tool_loop is not None else ""
        parts = [facts, self._profile(), context.block, caveats]
        return "\n\n".join(part for part in parts if part)

    def _facts(self) -> str:
        if self.facts is None:
            return ""
        try:
            return self.facts()
        except Exception as exc:  # noqa: BLE001 - an answer without the clock beats no answer
            log.warning("orchestrator.facts_failed", error=str(exc))
            return ""

    def _profile(self) -> str:
        """What Nox knows about the user, or nothing: a broken profile must not fail the turn."""
        if self.profile_block is None:
            return ""
        try:
            return self.profile_block()
        except Exception as exc:  # noqa: BLE001 - the answer matters more than the profile
            log.warning("orchestrator.profile_block_failed", error=str(exc))
            return ""

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
    def _split(buffer: str, *, final: bool, first: bool = False) -> tuple[list[str], str]:
        """Complete sentences out of `buffer`, and the rest.

        With `first`, a long opening sentence is cut at its last clause boundary (comma, colon,
        dash) once it is long enough to be worth saying on its own: the listener hears the first
        words while the model is still writing the end of that sentence.
        """
        parts = [p for p in _SENTENCE_END.split(buffer) if p is not None]
        if final:
            return [p.strip() for p in parts if p.strip()], ""
        if len(parts) <= 1:
            if first and len(buffer) >= FIRST_CLAUSE_MIN_CHARS:
                cut = max(buffer.rfind(mark) for mark in _CLAUSE_MARKS)
                if cut >= FIRST_CLAUSE_MIN_CHARS // 2:
                    head = buffer[: cut + 1].strip()
                    return [head], buffer[cut + 1 :].lstrip()
            return [], buffer
        return [p.strip() for p in parts[:-1] if p.strip()], parts[-1]

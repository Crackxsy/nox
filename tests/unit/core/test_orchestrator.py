from __future__ import annotations

import asyncio

import pytest

from nox.ai.base import AiRole
from nox.ai.fastpath import FastPath
from nox.core.events import E, Event
from nox.core.orchestrator import Orchestrator, OrchestratorConfig, TurnContext
from tests.unit.fakes import FakeBus, FakeRouter, FakeSpeaker, FakeState, FakeTurns

#: 30 words, no marker: the length half of the escalation rule without the memory half.
LONG_UNGROUNDED_QUESTION = (
    "Meine naechtliche Sicherung bricht seit dem letzten Update jedes Mal nach ungefaehr zwanzig "
    "Minuten ohne Fehlermeldung ab, obwohl genuegend Speicherplatz frei ist; was sind die drei "
    "plausibelsten Ursachen und wie pruefe ich sie der Reihe nach?"
)


async def _context(block: str, relevance: float) -> TurnContext:
    return TurnContext(block=block, relevance=relevance, items=1 if block else 0)


class AllowPolicy:
    def __init__(self, allow: bool = True) -> None:
        self.allow = allow

    def allows_memory_write(self) -> bool:
        return self.allow


def make(**kw):
    bus = FakeBus()
    state = FakeState()
    router = FakeRouter(bus)
    speaker = FakeSpeaker()
    turns = FakeTurns()
    orch = Orchestrator(
        bus=bus,
        state=state,
        router=router,
        speaker=speaker,
        turns=turns,
        memory_policy=kw.pop("policy", AllowPolicy()),
        system_prompt=lambda: "You are Nox.",
        config=OrchestratorConfig(**kw),
    )
    return orch, bus, router, speaker, turns


async def test_text_turn_streams_sentences_to_speaker():
    orch, bus, router, speaker, turns = make()
    await orch.start()
    turn = await orch.handle_text("Erklär mir kurz Python")
    assert turn.response.startswith("Hallo.")
    assert [s.text for s in speaker.said] == ["Hallo.", "Ich bin Nox!", "Wie geht es dir?"]
    assert turn.provider == "fake" and not turn.degraded
    assert [r[1] for r in turns.rows] == ["user", "assistant"]
    assert router.requests[0].messages[0].role == "system"
    assert router.requests[0].privacy_mode == "balanced"
    await orch.stop()
    assert E.SESSION_ENDED in bus.names()


async def test_transcript_event_triggers_turn_only_when_addressed():
    orch, bus, router, speaker, _ = make()
    await orch.start()
    await bus.publish(
        Event(
            name=E.VOICE_TRANSCRIPT_READY,
            payload={
                "text": "egal",
                "language": "de",
                "confidence": 0.9,
                "addressed_to_nox": False,
                "duration_ms": 1,
                "latency_ms": 1,
            },
        )
    )
    assert router.requests == []
    await bus.publish(
        Event(
            name=E.VOICE_TRANSCRIPT_READY,
            payload={
                "text": "Nox, erklär mir kurz Python",
                "language": "de",
                "confidence": 0.9,
                "addressed_to_nox": True,
                "duration_ms": 1,
                "latency_ms": 1,
            },
        )
    )
    assert orch.voice_turn is not None  # the turn runs outside bus delivery (2026-09-15 deadlock)
    await orch.voice_turn
    assert len(router.requests) == 1
    assert speaker.said
    await orch.stop()


async def test_kill_switch_cancels_and_blocks():
    orch, bus, router, speaker, _ = make()
    router.delay = 0.05
    await orch.start()
    task = asyncio.create_task(orch.handle_text("lange Antwort bitte"))
    await asyncio.sleep(0.08)
    await bus.publish(Event(name=E.SECURITY_KILL_SWITCH, payload={"by": "hotkey"}))
    with pytest.raises((asyncio.CancelledError, RuntimeError)):
        await task
    assert "kill_switch" in speaker.interrupts
    with pytest.raises(RuntimeError):
        await orch.handle_text("noch was")
    await bus.publish(Event(name=E.SYSTEM_STARTED))
    turn = await orch.handle_text("wieder da")
    assert turn.response
    await orch.stop()


async def test_memory_policy_blocks_recording():
    orch, _, _, _, turns = make(policy=AllowPolicy(False))
    await orch.start()
    await orch.handle_text("privat")
    assert turns.rows == []
    await orch.stop()


async def test_history_is_passed_to_router():
    orch, _, router, _, _ = make(history_turns=4)
    await orch.start()
    await orch.handle_text("eins")
    await orch.handle_text("zwei")
    roles = [m.role for m in router.requests[1].messages]
    assert roles == ["system", "user", "assistant", "user"]
    await orch.stop()


def test_sentence_split():
    sentences, rest = Orchestrator._split("Hallo. Wie geht", final=False)
    assert sentences == ["Hallo."] and rest == "Wie geht"
    sentences, rest = Orchestrator._split("Hallo. Wie geht", final=True)
    assert sentences == ["Hallo.", "Wie geht"] and rest == ""


# -- fast path, timings, memory context and escalation ---------------------------------------------


async def test_a_greeting_is_answered_without_asking_a_model():
    orch, _, router, speaker, turns = make()
    await orch.start()
    turn = await orch.handle_text("Hallo")
    assert router.requests == []  # no model was asked
    assert turn.fast_path == "greeting"
    assert turn.provider == "fastpath"  # the badge says so instead of naming a model
    assert turn.response.strip()
    # spoken sentence by sentence, exactly like a streamed answer
    assert " ".join(s.text for s in speaker.said) == turn.response
    assert [r[1] for r in turns.rows] == ["user", "assistant"]
    assert turn.timings.total_ms < 50.0
    await orch.stop()


async def test_the_fast_path_streams_its_answer_to_the_chunk_callback():
    orch, _, _, _, _ = make()
    await orch.start()
    seen: list[str] = []
    turn = await orch.handle_text("Danke", on_chunk=seen.append)
    assert seen == [turn.response]
    await orch.stop()


async def test_a_real_question_still_reaches_the_router():
    orch, _, router, _, _ = make()
    await orch.start()
    turn = await orch.handle_text("Hallo, wie konfiguriere ich den Import?")
    assert len(router.requests) == 1
    assert turn.fast_path == ""
    assert turn.provider == "fake"
    await orch.stop()


async def test_a_disabled_fast_path_sends_even_a_greeting_to_the_router():
    orch, _, router, _, _ = make()
    orch.fast_path = FastPath(enabled=False)
    await orch.start()
    await orch.handle_text("Hallo")
    assert len(router.requests) == 1
    await orch.stop()


async def test_memory_context_is_appended_to_the_system_prompt_and_timed():
    orch, _, router, _, _ = make()
    orch.context_provider = lambda _q: _context("## Memory context\n[vault:n.md#1] Notiz", 0.81)
    await orch.start()
    await orch.handle_text("Was steht in meinen Notizen?")
    system = router.requests[0].messages[0].content
    assert system.endswith("## Memory context\n[vault:n.md#1] Notiz")
    assert system.startswith("You are Nox.")
    await orch.stop()


async def test_a_failing_context_provider_degrades_to_no_context_not_a_failed_turn():
    orch, _, router, _, _ = make()

    async def broken(_query: str):
        raise RuntimeError("retrieval is down")

    orch.context_provider = broken
    await orch.start()
    turn = await orch.handle_text("Was steht in meinen Notizen?")
    assert turn.response  # the turn still answered
    assert router.requests[0].messages[0].content == "You are Nox."
    await orch.stop()


async def test_a_long_ungrounded_question_is_sent_with_the_reasoning_role():
    orch, _, router, _, _ = make()
    orch.context_provider = lambda _q: _context("", 0.0)
    await orch.start()
    turn = await orch.handle_text(LONG_UNGROUNDED_QUESTION)
    assert router.requests[0].role is AiRole.REASON
    assert "long_question_without_grounding" in turn.escalated
    await orch.stop()


async def test_the_same_question_stays_on_the_chat_chain_when_memory_grounds_it():
    orch, _, router, _, _ = make()
    orch.context_provider = lambda _q: _context("## Memory context\n[vault:n.md#1] Notiz", 0.82)
    await orch.start()
    turn = await orch.handle_text(LONG_UNGROUNDED_QUESTION)
    assert router.requests[0].role is AiRole.CHAT
    assert turn.escalated == ""
    await orch.stop()


async def test_every_stage_of_a_model_turn_is_timed():
    orch, _, _, _, _ = make()
    orch.context_provider = lambda _q: _context("## Memory context\n[vault:n.md#1] Notiz", 0.8)
    await orch.start()
    turn = await orch.handle_text("Erklär mir kurz Python")
    timings = turn.timings
    assert timings.context_ms >= 0.0
    assert timings.prompt_build_ms >= 0.0
    assert 0.0 < timings.first_token_ms <= timings.total_ms
    assert timings.first_token_ms <= timings.first_sentence_ms <= timings.total_ms
    await orch.stop()

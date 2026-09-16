from __future__ import annotations

import asyncio

import pytest

from nox.core.events import E, Event
from nox.core.orchestrator import Orchestrator, OrchestratorConfig
from tests.unit.fakes import FakeBus, FakeRouter, FakeSpeaker, FakeState, FakeTurns


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
    turn = await orch.handle_text("Hallo Nox")
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
                "text": "Nox, hallo",
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

from __future__ import annotations

import asyncio

import pytest

from nox.core.events import E, Event
from nox.core.state import EXPRESSIONS, Mood, PetFunctional
from nox.pet.service import PetService, expression_for
from tests.unit.fakes import FakeBus, FakeState


@pytest.fixture
async def pet():
    bus, state = FakeBus(), FakeState()
    svc = PetService(bus, state, tick_seconds=1000)
    await svc.start()
    yield svc, bus, state
    await svc.stop()


async def _pub(bus: FakeBus, name: str, **payload):
    await bus.publish(Event(name=name, payload=payload))


async def test_start_emits_idle(pet):
    svc, bus, state = pet
    assert svc.functional is PetFunctional.IDLE
    assert bus.names()[-1] == E.PET_STATE_CHANGED
    assert state.get("assistant.pet_functional") == "idle"


async def test_voice_flow_listening_thinking_speaking_idle(pet):
    svc, bus, _ = pet
    await _pub(bus, E.VOICE_INPUT_STARTED)
    assert svc.functional is PetFunctional.LISTENING
    await _pub(
        bus, E.AI_REQUEST_STARTED, request_id="r", provider="p", role="chat", mode="companion"
    )
    assert svc.functional is PetFunctional.THINKING
    await _pub(bus, E.TTS_STARTED, text="hi", channel="private", engine="e", utterance_id="u")
    assert svc.functional is PetFunctional.SPEAKING
    await _pub(bus, E.AI_RESPONSE_READY, request_id="r", provider="p", text="hi", latency_ms=1)
    assert svc.functional is PetFunctional.SPEAKING  # still speaking
    await _pub(bus, E.TTS_FINISHED)
    assert svc.functional is PetFunctional.IDLE


async def test_kill_switch_forces_unavailable_and_blocks_updates(pet):
    svc, bus, _ = pet
    await _pub(bus, E.SECURITY_KILL_SWITCH, by="hotkey")
    assert svc.functional is PetFunctional.UNAVAILABLE
    await _pub(bus, E.VOICE_INPUT_STARTED)
    assert svc.functional is PetFunctional.UNAVAILABLE
    await _pub(bus, E.SYSTEM_STARTED)
    assert svc.functional is PetFunctional.IDLE


async def test_muted_and_privacy_zone_precedence(pet):
    svc, bus, _ = pet
    await _pub(bus, E.VOICE_MUTED, muted=True)
    assert svc.functional is PetFunctional.MUTED
    await _pub(bus, "privacy.zone_changed", active=True)
    assert svc.functional is PetFunctional.PRIVACY
    await _pub(bus, "privacy.zone_changed", active=False)
    assert svc.functional is PetFunctional.MUTED
    await _pub(bus, E.VOICE_MUTED, muted=False)
    assert svc.functional is PetFunctional.IDLE


async def test_ai_failure_without_fallback_shows_error(pet):
    svc, bus, _ = pet
    await _pub(
        bus, E.AI_REQUEST_FAILED, request_id="r", provider="p", error="boom", fallback_to="ollama"
    )
    assert svc.functional is PetFunctional.IDLE
    await _pub(bus, E.AI_REQUEST_FAILED, request_id="r", provider="p", error="boom")
    assert svc.functional is PetFunctional.ERROR
    await asyncio.sleep(0)


async def test_interaction_nudges_mood(pet):
    svc, bus, _ = pet
    before = svc.mood.affection
    await _pub(bus, E.PET_INTERACTION, type="click")
    assert svc.mood.affection > before


def test_decay_moves_toward_baseline():
    svc = PetService(FakeBus(), FakeState(), baseline=Mood())
    svc._nudge(stress=+0.5)
    high = svc.mood.stress
    svc.decay_once()
    assert svc.mood.stress < high


@pytest.mark.parametrize("functional", list(PetFunctional))
def test_expression_always_in_catalog(functional):
    expr, intensity = expression_for(functional, Mood(), "companion")
    assert expr in EXPRESSIONS
    assert 0.0 <= intensity <= 1.0

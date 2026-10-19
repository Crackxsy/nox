"""A turn's privacy is decided for the whole turn: sticky, and shared by question and answer.

A switch of the privacy mode in the middle of an answer must not store the question one way and
the answer another; a moment without cloud permission marks both private, a moment without
memory permission stores neither.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from nox.ai.base import AiChunk, AiRequest
from nox.core.orchestrator import Orchestrator
from tests.unit.fakes import FakeBus, FakeRouter, FakeSpeaker, FakeState, FakeTurns


@dataclass
class Switches:
    cloud: bool
    memory: bool = True


class Policy:
    def __init__(self, switches: Switches) -> None:
        self._switches = switches

    def allows_memory_write(self) -> bool:
        return self._switches.memory


class SwitchingRouter(FakeRouter):
    """Streams two chunks and flips the privacy switches between them."""

    def __init__(self, bus: FakeBus, flip: Callable[[], None]) -> None:
        super().__init__(bus)
        self._flip = flip

    async def stream(self, request: AiRequest) -> AsyncIterator[AiChunk]:  # type: ignore[override]
        self.requests.append(request)
        yield AiChunk(request_id=request.request_id, delta="Die Antwort ")
        self._flip()
        yield AiChunk(request_id=request.request_id, delta="kommt.", done=True)


def _orchestrator(switches: Switches, flip: Callable[[], None]) -> tuple[Orchestrator, FakeTurns]:
    bus = FakeBus()
    turns = FakeTurns()
    orchestrator = Orchestrator(
        bus=bus,
        state=FakeState(),
        router=SwitchingRouter(bus, flip),
        speaker=FakeSpeaker(),
        turns=turns,
        memory_policy=Policy(switches),
        system_prompt=lambda: "You are Nox.",
        cloud_allowed=lambda: switches.cloud,
    )
    return orchestrator, turns


async def test_offline_question_then_full_during_the_answer_stores_both_private() -> None:
    switches = Switches(cloud=False)
    orchestrator, turns = _orchestrator(switches, lambda: setattr(switches, "cloud", True))

    await orchestrator.handle_text("Eine Frage im Offline-Modus", speak=False)

    assert [row[1] for row in turns.rows] == ["user", "assistant"]
    assert turns.private == [True, True]


async def test_a_switch_away_from_the_cloud_during_the_answer_also_marks_the_question() -> None:
    switches = Switches(cloud=True)
    orchestrator, turns = _orchestrator(switches, lambda: setattr(switches, "cloud", False))

    await orchestrator.handle_text("Eine Frage im vollen Modus", speak=False)

    assert turns.private == [True, True]


async def test_a_question_in_private_mode_is_not_stored_even_if_the_mode_changes() -> None:
    switches = Switches(cloud=False, memory=False)

    def back_to_full() -> None:
        switches.cloud, switches.memory = True, True

    orchestrator, turns = _orchestrator(switches, back_to_full)

    await orchestrator.handle_text("Eine Frage im privaten Modus", speak=False)

    assert turns.rows == []


async def test_a_fast_path_answer_offline_is_stored_private_too() -> None:
    switches = Switches(cloud=False)
    orchestrator, turns = _orchestrator(switches, lambda: None)

    turn = await orchestrator.handle_text("Hallo", speak=False)

    assert turn.fast_path, "a greeting is answered without a model"
    assert turns.private == [True, True]


async def test_an_ordinary_turn_is_stored_public() -> None:
    switches = Switches(cloud=True)
    orchestrator, turns = _orchestrator(switches, lambda: None)

    await orchestrator.handle_text("Eine normale Frage", speak=False)

    assert turns.private == [False, False]

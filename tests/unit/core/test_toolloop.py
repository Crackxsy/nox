"""Tools in a conversation: the model asks, the tool loop runs it under a budget, the user hears
only the answer.

Native calls arrive as data (`AiChunk.tool_calls`), so the failure to guard against is no longer a
protocol line read aloud but a loop that spins, a call outside the offer, or a call that survives
the end of its turn.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import BaseModel

from nox.ai.base import AiChunk, AiRequest, ToolCallRequest
from nox.ai.tooluse import OfferedTool, Unavailable
from nox.core.orchestrator import GAVE_UP_ON_TOOLS, TOOL_FILLER, Orchestrator, OrchestratorConfig
from nox.core.toolloop import (
    REFUSED_BUDGET,
    REFUSED_NOT_OFFERED,
    REFUSED_TURN_OVER,
    ToolLoop,
)
from nox.core.turnbudget import TurnBudget
from nox.voice.base import TtsRequest
from tests.unit.fakes import FakeBus, FakeRouter, FakeSpeaker, FakeState, FakeTurns

LIGHT = OfferedTool(name="home.light", description="Dim the lights.", schema={})
Step = str | list[ToolCallRequest]


def call(name: str = "home.light", **arguments: Any) -> list[ToolCallRequest]:
    return [ToolCallRequest(id="c1", name=name, arguments=arguments)]


class Outcome(BaseModel):
    ok: bool
    data: dict[str, Any] | None = None
    error: str | None = None


class ScriptedRouter(FakeRouter):
    """One scripted step per model call: a text answer, or tool calls for Nox to run."""

    def __init__(self, bus: FakeBus, steps: list[Step]) -> None:
        super().__init__(bus)
        self.steps = list(steps)

    async def stream(self, request: AiRequest) -> AsyncIterator[AiChunk]:  # type: ignore[override]
        step = self.steps.pop(0) if self.steps else "Fertig."
        if isinstance(step, list):
            self.requests.append(request)
            yield AiChunk(request_id=request.request_id, delta="", done=True, tool_calls=step)
            return
        self.text = step
        async for chunk in super().stream(request):
            yield chunk


class Recorder:
    """Stands in for the executor: records the calls and answers from a script."""

    def __init__(self, outcome: Outcome | None = None, gate: asyncio.Event | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.outcome = outcome or Outcome(ok=True, data={"state": "on"})
        self.gate = gate
        self.started = asyncio.Event()

    async def __call__(self, name: str, arguments: dict[str, Any]) -> Outcome:
        self.calls.append((name, arguments))
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        return self.outcome


class SignallingSpeaker(FakeSpeaker):
    def __init__(self) -> None:
        super().__init__()
        self.spoke = asyncio.Event()

    async def say(self, request: TtsRequest) -> None:
        await super().say(request)
        self.spoke.set()


class Policy:
    def allows_memory_write(self) -> bool:
        return True


def make(
    steps: list[Step],
    *,
    recorder: Recorder | None = None,
    tools: Any = (LIGHT,),
    max_calls: int = 20,
    unavailable: Any = None,
    speaker: FakeSpeaker | None = None,
    **kw: Any,
) -> tuple[Orchestrator, ScriptedRouter, FakeSpeaker, Recorder]:
    bus = FakeBus()
    router = ScriptedRouter(bus, steps)
    speaker = speaker or FakeSpeaker()
    recorder = recorder or Recorder()
    orch = Orchestrator(
        bus=bus,
        state=FakeState(),
        router=router,
        speaker=speaker,
        turns=FakeTurns(),
        memory_policy=Policy(),
        system_prompt=lambda: "You are Nox.",
        config=OrchestratorConfig(**kw),
        tool_loop=ToolLoop(
            offer=lambda: list(tools),
            call=recorder,
            unavailable=unavailable,
            budget_factory=lambda: TurnBudget(max_calls=max_calls),
        ),
    )
    return orch, router, speaker, recorder


# -- the conversation ---------------------------------------------------------------------------


async def test_a_turn_without_a_tool_costs_one_model_call() -> None:
    orch, router, speaker, recorder = make(["Hallo. Alles gut."])

    turn = await orch.handle_text("Erklaer mir kurz Python")

    assert turn.response == "Hallo. Alles gut."
    assert [s.text for s in speaker.said] == ["Hallo.", "Alles gut."]
    assert recorder.calls == [] and turn.tools_used == []
    assert len(router.requests) == 1


async def test_the_offer_travels_as_native_tool_definitions() -> None:
    orch, router, _, _ = make(["Ok."])

    await orch.handle_text("Licht?")

    assert [tool.name for tool in router.requests[0].tools] == ["home.light"]
    assert router.requests[0].tool_turn, "the turn token must reach the provider"
    assert "NOX_TOOL_CALL" not in router.requests[0].messages[0].content


async def test_the_model_can_reach_a_tool_and_then_answer() -> None:
    orch, _, _, recorder = make([call(on=True), "Licht ist an."])

    turn = await orch.handle_text("Mach das Licht an")

    assert recorder.calls == [("home.light", {"on": True})]
    assert turn.tools_used == ["home.light"]
    assert turn.response == "Licht ist an."


async def test_the_tool_result_is_handed_back_as_a_tool_message() -> None:
    orch, router, _, _ = make(
        [call(), "Ist an."], recorder=Recorder(Outcome(ok=True, data={"brightness": 30}))
    )

    await orch.handle_text("Licht")

    second = router.requests[1].messages
    assert second[-2].role == "assistant" and second[-2].tool_calls[0].name == "home.light"
    assert second[-1].role == "tool" and second[-1].tool_name == "home.light"
    assert "brightness" in second[-1].content


async def test_a_failing_tool_is_reported_to_the_model_not_to_the_user() -> None:
    orch, router, speaker, _ = make(
        [call(), "Ich komme nicht an die Lampe."],
        recorder=Recorder(Outcome(ok=False, error="no token stored")),
    )

    turn = await orch.handle_text("Licht an")

    assert "no token stored" in router.requests[1].messages[-1].content
    assert turn.response == "Ich komme nicht an die Lampe."
    assert "no token stored" not in " ".join(s.text for s in speaker.said)


async def test_a_tool_nobody_offered_never_reaches_the_executor() -> None:
    orch, router, _, recorder = make([call("file.delete", path="C:/"), "Das kann ich nicht."])

    turn = await orch.handle_text("Loesch mal was")

    assert recorder.calls == []
    assert REFUSED_NOT_OFFERED in router.requests[1].messages[-1].content
    assert turn.response == "Das kann ich nicht."


async def test_a_confused_model_cannot_spin() -> None:
    """Calls for ever: the budget ends the loop, the offer is withdrawn, the user hears a
    sentence."""
    orch, router, _, recorder = make([call()] * 10, max_calls=2)

    turn = await orch.handle_text("Licht an")

    assert len(recorder.calls) == 2, "the budget admits exactly max_calls calls"
    assert router.requests[-1].tools == [], "a spent budget withdraws the offer"
    assert len(router.requests) <= 2 + 2
    assert turn.response == GAVE_UP_ON_TOOLS["de"]


async def test_after_the_budget_the_model_is_asked_without_tools_and_answers() -> None:
    orch, router, _, recorder = make([call(), "Ich habe es versucht."], max_calls=1)

    turn = await orch.handle_text("Licht an")

    assert len(recorder.calls) == 1
    assert router.requests[1].tools == [] and router.requests[0].tools != []
    assert turn.response == "Ich habe es versucht."


async def test_without_an_offer_no_tools_are_sent() -> None:
    orch, router, _, _ = make(["Alles klar."], tools=())

    turn = await orch.handle_text("Erklaer mir kurz Python")

    assert router.requests[0].tools == []
    assert turn.response == "Alles klar."


async def test_what_cannot_be_done_goes_into_the_turn_context() -> None:
    orch, router, _, _ = make(
        ["Ok."],
        unavailable=lambda: [Unavailable(name="home.*", detail="no Home Assistant token")],
    )

    await orch.handle_text("Licht?")

    context = router.requests[0].context
    assert "- home.*: no Home Assistant token" in context
    assert "NOT AVAILABLE" not in router.requests[0].messages[0].content


async def test_a_broken_caveat_provider_still_leaves_the_offer_standing() -> None:
    def broken() -> list[Unavailable]:
        raise RuntimeError("report failed")

    orch, router, _, _ = make(["Ok."], unavailable=broken)

    await orch.handle_text("Licht?")

    assert [tool.name for tool in router.requests[0].tools] == ["home.light"]


# -- the "let me check" filler ------------------------------------------------------------------


async def test_a_slow_tool_with_nothing_said_gets_one_filler() -> None:
    release = asyncio.Event()
    speaker = SignallingSpeaker()
    recorder = Recorder(gate=release)
    orch, _, _, _ = make(
        [call(), "Licht ist an."], recorder=recorder, speaker=speaker, filler_delay_s=0.0
    )

    task = asyncio.create_task(orch.handle_text("Licht an"))
    await asyncio.wait_for(speaker.spoke.wait(), timeout=5)
    release.set()
    await task

    assert [s.text for s in speaker.said] == [TOOL_FILLER["de"], "Licht ist an."]


async def test_no_filler_when_the_model_already_spoke() -> None:
    class SpeaksFirst(ScriptedRouter):
        async def stream(self, request: AiRequest) -> AsyncIterator[AiChunk]:  # type: ignore[override]
            if not self.requests:
                self.requests.append(request)
                yield AiChunk(request_id=request.request_id, delta="Ich schau nach. ")
                yield AiChunk(request_id=request.request_id, delta="", done=True, tool_calls=call())
                return
            async for chunk in super().stream(request):
                yield chunk

    orch, _, speaker, _ = make([], filler_delay_s=0.0)
    orch.router = SpeaksFirst(FakeBus(), ["Erledigt."])

    await orch.handle_text("Licht an")

    assert TOOL_FILLER["de"] not in [s.text for s in speaker.said]
    assert [s.text for s in speaker.said] == ["Ich schau nach.", "Erledigt."]


async def test_no_filler_in_a_text_only_turn() -> None:
    orch, _, speaker, _ = make([call(), "Ok."], filler_delay_s=0.0)

    await orch.handle_text("Licht an", speak=False)

    assert speaker.said == []


# -- the loop itself ------------------------------------------------------------------------------


def _loop(recorder: Recorder | None = None, **budget: Any) -> ToolLoop:
    return ToolLoop(
        offer=lambda: [LIGHT],
        call=recorder or Recorder(),
        budget_factory=lambda: TurnBudget(**budget),
    )


async def test_a_token_finds_its_turn_and_dies_with_it() -> None:
    loop = _loop()
    turn = loop.begin("t1")

    assert loop.by_token(turn.token) is turn
    await loop.end(turn)

    assert loop.by_token(turn.token) is None
    run = await loop.execute(turn, "home.light", {})
    assert run.refused and run.error == REFUSED_TURN_OVER


async def test_every_turn_gets_a_new_token() -> None:
    loop = _loop()
    first, second = loop.begin("a"), loop.begin("b")

    assert first.token != second.token and len(first.token) >= 32
    assert loop.by_token("x" * 43) is None


async def test_ending_a_turn_cancels_its_running_call() -> None:
    release = asyncio.Event()
    recorder = Recorder(gate=release)
    loop = _loop(recorder)
    turn = loop.begin("t")

    running = asyncio.create_task(loop.execute(turn, "home.light", {}))
    await asyncio.wait_for(recorder.started.wait(), timeout=5)
    await loop.end(turn)
    run = await running

    assert run.refused and run.error == REFUSED_TURN_OVER
    assert turn.used == []


async def test_end_all_closes_every_open_turn() -> None:
    loop = _loop()
    turns = [loop.begin("a"), loop.begin("b")]

    await loop.end_all()

    assert all(t.closed for t in turns) and loop.open_turns() == []


async def test_the_budget_refuses_the_call_after_the_last_one() -> None:
    recorder = Recorder()
    loop = _loop(recorder, max_calls=2)
    turn = loop.begin("t")

    runs = [await loop.execute(turn, "home.light", {"n": i}) for i in range(3)]

    assert [r.refused for r in runs] == [False, False, True]
    assert runs[2].error.startswith(REFUSED_BUDGET)
    assert len(recorder.calls) == 2


async def test_the_same_failing_call_is_not_retried_a_fourth_time() -> None:
    recorder = Recorder(Outcome(ok=False, error="boom"))
    loop = _loop(recorder)
    turn = loop.begin("t")

    runs = [await loop.execute(turn, "home.light", {"x": 1}) for _ in range(4)]

    assert [r.refused for r in runs] == [False, False, False, True]
    other = await loop.execute(turn, "home.light", {"x": 2})
    assert not other.refused, "a different call is still allowed"


async def test_a_crashing_tool_becomes_a_failed_run() -> None:
    async def crash(_name: str, _arguments: dict[str, Any]) -> Outcome:
        raise RuntimeError("boom")

    loop = ToolLoop(offer=lambda: [LIGHT], call=crash)
    run = await loop.execute(loop.begin("t"), "home.light", {})

    assert not run.ok and not run.refused and run.error == "RuntimeError"


@pytest.mark.parametrize(("calls", "expected"), [(0, False), (2, True)])
def test_the_wall_clock_exhausts_the_budget(calls: int, expected: bool) -> None:
    now = [0.0]
    budget = TurnBudget(max_calls=5, wall_clock_s=10.0, clock=lambda: now[0])
    for _ in range(calls):
        assert budget.admit("t", {}) is None
    now[0] = 10.0 if expected else 9.0

    refusal = budget.admit("t", {})

    assert (refusal is not None) is expected
    assert budget.exhausted is expected

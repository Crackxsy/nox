"""Tools in a conversation: the model asks, the gate runs it, the user never hears the protocol.

The last part is why this file exists. The request travels as a line of text through the same
stream that carries the spoken answer, so the failure to guard against is not a wrong tool call -
it is Nox reading `NOX_TOOL_CALL {"name": ...}` aloud to the user.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from nox.ai.base import AiRequest
from nox.ai.tooluse import SENTINEL, OfferedTool
from nox.core.orchestrator import GAVE_UP_ON_TOOLS, Orchestrator, OrchestratorConfig
from nox.core.toolloop import ToolGate
from tests.unit.fakes import FakeBus, FakeRouter, FakeSpeaker, FakeState, FakeTurns

LIGHT = OfferedTool(name="home.light", description="Dim the lights.", schema={})


def directive(payload: str) -> str:
    return f"{SENTINEL} {payload}"


class Outcome(BaseModel):
    ok: bool
    data: dict[str, Any] | None = None
    error: str | None = None


class ScriptedRouter(FakeRouter):
    """One scripted answer per model call, so a multi-round turn can be written down."""

    def __init__(self, bus: FakeBus, answers: list[str]) -> None:
        super().__init__(bus)
        self.answers = list(answers)

    async def stream(self, request: AiRequest):  # type: ignore[override]
        self.text = self.answers.pop(0) if self.answers else "Fertig."
        async for chunk in super().stream(request):
            yield chunk


class Recorder:
    """Stands in for the executor: records the calls and answers from a script."""

    def __init__(self, outcome: Outcome | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.outcome = outcome or Outcome(ok=True, data={"state": "on"})

    async def __call__(self, name: str, arguments: dict[str, Any]) -> Outcome:
        self.calls.append((name, arguments))
        return self.outcome


class Policy:
    def allows_memory_write(self) -> bool:
        return True


def make(
    answers: list[str],
    *,
    recorder: Recorder | None = None,
    tools: Any = (LIGHT,),
    rounds: int = 3,
    **kw: Any,
):
    bus = FakeBus()
    router = ScriptedRouter(bus, answers)
    speaker = FakeSpeaker()
    turns = FakeTurns()
    call = recorder or Recorder()
    orch = Orchestrator(
        bus=bus,
        state=FakeState(),
        router=router,
        speaker=speaker,
        turns=turns,
        memory_policy=Policy(),
        system_prompt=lambda: "You are Nox.",
        config=OrchestratorConfig(**kw),
        tool_gate=ToolGate(offer=lambda: list(tools), call=call, max_rounds=rounds),
    )
    return orch, router, speaker, call, turns


async def test_a_turn_without_a_tool_is_unchanged() -> None:
    """The overwhelming majority of turns. The gate must cost them nothing but a prompt section."""
    orch, router, speaker, call, _ = make(["Hallo. Alles gut."])

    # Not a question the deterministic fast path answers - that path never reaches a model at all.
    turn = await orch.handle_text("Erklaer mir kurz Python")

    assert turn.response == "Hallo. Alles gut."
    assert [s.text for s in speaker.said] == ["Hallo.", "Alles gut."]
    assert call.calls == []
    assert turn.tools_used == []
    assert len(router.requests) == 1, "an ordinary answer must not cost a second model call"


async def test_the_model_can_reach_a_tool_and_then_answer() -> None:
    orch, router, speaker, call, _ = make(
        [directive('{"name": "home.light", "arguments": {"on": true}}'), "Licht ist an."]
    )

    turn = await orch.handle_text("Mach das Licht an")

    assert call.calls == [("home.light", {"on": True})]
    assert turn.tools_used == ["home.light"]
    assert turn.response == "Licht ist an."


async def test_the_user_never_hears_the_protocol() -> None:
    """The whole reason the stream is held back at the start of every tool-enabled turn."""
    orch, router, speaker, call, turns = make(
        [directive('{"name": "home.light", "arguments": {}}'), "Erledigt."]
    )

    turn = await orch.handle_text("Licht")

    spoken = " ".join(s.text for s in speaker.said)
    assert SENTINEL not in spoken
    assert SENTINEL not in turn.response
    # And it is not written into the history either, where the next turn would read it back.
    assert all(SENTINEL not in row[2] for row in turns.rows)


async def test_the_tool_result_is_handed_to_the_model() -> None:
    orch, router, _, _, _ = make(
        [directive('{"name": "home.light", "arguments": {}}'), "Ist an."],
        recorder=Recorder(Outcome(ok=True, data={"brightness": 30})),
    )

    await orch.handle_text("Licht")

    second = router.requests[1].messages
    assert second[-1].role == "user"
    assert "brightness" in second[-1].content


async def test_a_failing_tool_is_reported_to_the_model_not_to_the_user() -> None:
    """The user should hear a sentence, not an error string the model never saw."""
    orch, router, speaker, _, _ = make(
        [directive('{"name": "home.light", "arguments": {}}'), "Ich komme nicht an die Lampe."],
        recorder=Recorder(Outcome(ok=False, error="no token stored")),
    )

    turn = await orch.handle_text("Licht an")

    assert "no token stored" in router.requests[1].messages[-1].content
    assert turn.response == "Ich komme nicht an die Lampe."
    assert "no token stored" not in " ".join(s.text for s in speaker.said)


async def test_a_tool_nobody_offered_never_reaches_the_executor() -> None:
    orch, router, _, call, _ = make(
        [directive('{"name": "file.delete", "arguments": {"path": "C:/"}}'), "Das kann ich nicht."]
    )

    turn = await orch.handle_text("Loesch mal was")

    assert call.calls == [], "the name check has to happen before the executor, not inside it"
    assert "file.delete" in router.requests[1].messages[-1].content
    assert turn.response == "Das kann ich nicht."


async def test_a_confused_model_cannot_spin() -> None:
    """Directives for ever: the loop has to end and the user has to get something to hear."""
    endless = directive('{"name": "home.light", "arguments": {}}')
    orch, router, speaker, call, _ = make([endless] * 4, rounds=2)

    turn = await orch.handle_text("Licht an")

    assert len(router.requests) == 3, "two tool rounds plus one final answer"
    assert len(call.calls) == 2, "the round that had no offer must not run a tool either"
    # Nothing usable came back, so Nox says one sentence of its own rather than the protocol.
    assert turn.response == GAVE_UP_ON_TOOLS["de"]
    assert SENTINEL not in " ".join(s.text for s in speaker.said)


async def test_the_final_answer_is_used_when_the_model_stops_asking() -> None:
    endless = directive('{"name": "home.light", "arguments": {}}')
    orch, _, _, call, _ = make([endless, endless, "Ich habe es versucht."], rounds=2)

    turn = await orch.handle_text("Licht an")

    assert len(call.calls) == 2
    assert turn.response == "Ich habe es versucht."


async def test_the_last_round_is_asked_without_any_tools() -> None:
    """Taking the offer away is what forces prose, instead of a fourth directive."""
    endless = directive('{"name": "home.light", "arguments": {}}')
    orch, router, _, _, _ = make([endless, "Fertig."], rounds=1)

    await orch.handle_text("Licht an")

    assert SENTINEL in router.requests[0].messages[0].content
    assert SENTINEL not in router.requests[-1].messages[0].content


async def test_without_an_offer_the_protocol_is_never_mentioned() -> None:
    """A privacy mode or profile that leaves nothing usable must not advertise a tool protocol."""
    orch, router, _, call, _ = make(["Alles klar."], tools=())

    turn = await orch.handle_text("Erklaer mir kurz Python")

    assert SENTINEL not in router.requests[0].messages[0].content
    assert len(router.requests) == 1
    assert turn.response == "Alles klar."

"""Ollama tool calling against a fake Ollama server: the native `tools` field for a model that
declares it, the text-sentinel adapter for one that does not, and the whole loop through the
orchestrator - the protocol line never reaches the user in either case.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from nox.ai.base import AiChunk, AiRequest, AiRole, Message, ToolCallRequest, ToolDefinition
from nox.ai.config import OllamaConfig
from nox.ai.providers.ollama import OllamaProvider
from nox.ai.tooluse import OfferedTool
from nox.core.orchestrator import Orchestrator
from nox.core.toolloop import ToolLoop
from tests.unit.fakes import FakeBus, FakeSpeaker, FakeState, FakeTurns

LIGHT = ToolDefinition(
    name="home.light",
    description="Switch a light.",
    input_schema={"type": "object", "properties": {"on": {"type": "boolean"}}},
    asks_first=True,
)


class FakeOllama:
    """`/api/show` answers the capabilities; `/api/chat` streams scripted NDJSON answers."""

    def __init__(self, *, tools_capability: bool, answers: list[list[dict[str, Any]]]) -> None:
        self.capabilities = ["completion", "tools"] if tools_capability else ["completion"]
        self.answers = list(answers)
        self.chat_bodies: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": self.capabilities})
        if request.url.path == "/api/chat":
            self.chat_bodies.append(json.loads(request.content))
            chunks = self.answers.pop(0)
            lines = "\n".join(json.dumps(c) for c in chunks) + "\n"
            return httpx.Response(200, content=lines.encode())
        return httpx.Response(404)

    def factory(self) -> Callable[[], httpx.AsyncClient]:
        return lambda: httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def text(*parts: str) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = [{"message": {"content": p}, "done": False} for p in parts]
    chunks.append({"message": {"content": ""}, "done": True})
    return chunks


def native_call(name: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
    call = {"function": {"name": name, "arguments": arguments}}
    return [
        {"message": {"content": "", "tool_calls": [call]}, "done": False},
        {"message": {"content": ""}, "done": True},
    ]


def _request(**kw: Any) -> AiRequest:
    return AiRequest(
        request_id="r",
        role=AiRole.CHAT,
        messages=[
            Message(role="system", content="You are Nox."),
            Message(role="user", content="Licht an"),
        ],
        tools=[LIGHT],
        **kw,
    )


async def _chunks(provider: OllamaProvider, request: AiRequest) -> list[AiChunk]:
    return [chunk async for chunk in provider.stream(request)]


async def test_a_model_with_tool_support_gets_the_native_tools_field() -> None:
    fake = FakeOllama(tools_capability=True, answers=[native_call("home-light", {"on": True})])
    provider = OllamaProvider(OllamaConfig(), fake.factory())

    chunks = await _chunks(provider, _request(context="Current facts:\n- time: 9"))

    body = fake.chat_bodies[0]
    assert body["tools"][0]["function"]["name"] == "home-light"
    assert "confirmation" in body["tools"][0]["function"]["description"]
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][0]["content"].endswith("- time: 9"), "the turn context is sent"
    assert "NOX_TOOL_CALL" not in body["messages"][0]["content"]
    assert chunks[-1].done and chunks[-1].tool_calls == [
        ToolCallRequest(id="call_0", name="home.light", arguments={"on": True})
    ]


async def test_tool_results_go_back_as_tool_messages() -> None:
    fake = FakeOllama(tools_capability=True, answers=[text("Ist an.")])
    provider = OllamaProvider(OllamaConfig(), fake.factory())
    request = _request()
    request.messages += [
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCallRequest(name="home.light", arguments={"on": True})],
        ),
        Message(role="tool", content="Result of home.light: done", tool_name="home.light"),
    ]

    await _chunks(provider, request)

    messages = fake.chat_bodies[0]["messages"]
    assert messages[-2]["tool_calls"][0]["function"]["name"] == "home-light"
    assert messages[-1] == {
        "role": "tool",
        "content": "Result of home.light: done",
        "tool_name": "home-light",
    }


async def test_a_model_without_tool_support_gets_the_offer_as_text_and_a_hidden_directive() -> None:
    directive = 'NOX_TOOL_CALL {"name": "home.light", "arguments": {"on": true}}'
    fake = FakeOllama(
        tools_capability=False, answers=[text(directive[:5], directive[5:20], directive[20:])]
    )
    provider = OllamaProvider(OllamaConfig(), fake.factory())

    chunks = await _chunks(provider, _request())

    body = fake.chat_bodies[0]
    assert "tools" not in body
    assert "NOX_TOOL_CALL" in body["messages"][0]["content"]
    assert "".join(c.delta for c in chunks) == "", "the directive is never shown or spoken"
    assert chunks[-1].tool_calls[0].name == "home.light"


async def test_prose_from_a_sentinel_model_streams_as_soon_as_it_is_clearly_prose() -> None:
    fake = FakeOllama(tools_capability=False, answers=[text("Das ", "Licht ", "ist an.")])
    provider = OllamaProvider(OllamaConfig(), fake.factory())

    chunks = await _chunks(provider, _request())

    assert "".join(c.delta for c in chunks) == "Das Licht ist an."
    assert chunks[-1].tool_calls == []


class Outcome(BaseModel):
    ok: bool
    data: dict[str, Any] | None = None
    error: str | None = None


class Policy:
    def allows_memory_write(self) -> bool:
        return True


@pytest.mark.parametrize("native", [True, False])
async def test_the_orchestrator_runs_the_call_and_the_user_hears_only_the_answer(
    native: bool,
) -> None:
    first = (
        native_call("home-light", {"on": True})
        if native
        else text('NOX_TOOL_CALL {"name": "home.light", "arguments": {"on": true}}')
    )
    fake = FakeOllama(tools_capability=native, answers=[first, text("Das Licht ist an.")])
    provider = OllamaProvider(OllamaConfig(), fake.factory())
    calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(name: str, arguments: dict[str, Any]) -> Outcome:
        calls.append((name, arguments))
        return Outcome(ok=True, data={"state": "on"})

    class DirectRouter:
        async def stream(self, request: AiRequest):  # type: ignore[no-untyped-def]
            async for chunk in provider.stream(request):
                yield chunk

        def explain(self, _request_id: str) -> str:
            return "ollama"

    speaker = FakeSpeaker()
    orchestrator = Orchestrator(
        bus=FakeBus(),
        state=FakeState(),
        router=DirectRouter(),  # type: ignore[arg-type]
        speaker=speaker,
        turns=FakeTurns(),
        memory_policy=Policy(),
        system_prompt=lambda: "You are Nox.",
        tool_loop=ToolLoop(
            offer=lambda: [OfferedTool(name="home.light", description="Switch a light.")],
            call=execute,
        ),
    )

    turn = await orchestrator.handle_text("Mach das Licht an")

    assert calls == [("home.light", {"on": True})]
    assert turn.response == "Das Licht ist an."
    assert [s.text for s in speaker.said] == ["Das Licht ist an."]
    assert "NOX_TOOL_CALL" not in turn.response

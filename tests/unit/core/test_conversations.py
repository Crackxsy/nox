"""One conversation per channel, cleared exactly when its context is discarded (re-review N1).

Before: every turn of the process - voice, every dashboard client, every phone - ran in one
conversation (`Orchestrator.session_id`), so one page read anywhere tainted everything, and
nothing ever cleared it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel

from nox.ai.base import AiChunk, AiRequest
from nox.ai.tooluse import OfferedTool
from nox.core.orchestrator import VOICE_CHANNEL, is_new_conversation_request
from nox.core.toolloop import ToolLoop
from tests.unit.core.test_orchestrator import make


class _Outcome(BaseModel):
    ok: bool = True
    data: dict[str, Any] | None = None
    error: str | None = None


class _Taint:
    """Stands in for the permission engine's taint calls."""

    def __init__(self) -> None:
        self.tainted: set[str] = set()
        self.cleared: list[str] = []
        self.user: dict[str, str] = {}
        self.marks: list[tuple[str, str | None]] = []

    def mark(self, conversation: str, content: str | None) -> None:
        self.marks.append((conversation, content))
        self.tainted.add(conversation)

    def clear(self, conversation: str) -> None:
        self.cleared.append(conversation)
        self.tainted.discard(conversation)

    def note(self, conversation: str, text: str) -> None:
        self.user[conversation] = text


def _with_tools(orch: Any) -> tuple[_Taint, list[str]]:
    taint = _Taint()
    begun: list[str] = []

    async def call(name: str, arguments: dict[str, Any]) -> _Outcome:
        return _Outcome(data={"text": "page"})

    def note(conversation: str, text: str) -> None:
        begun.append(conversation)  # called once per turn, from `ToolLoop.begin`
        taint.note(conversation, text)

    orch.tool_loop = ToolLoop(
        offer=lambda: [OfferedTool(name="web.fetch", description="", schema={})],
        call=call,
        taint=taint.mark,
        user_message=note,
        clear_taint=taint.clear,
    )
    return taint, begun


async def test_each_channel_is_its_own_conversation() -> None:
    orch, _, router, _, _ = make()
    _, begun = _with_tools(orch)
    await orch.start()
    await orch.handle_text("eins", channel=VOICE_CHANNEL)
    await orch.handle_text("zwei", channel="chat:dashboard-1")
    await orch.handle_text("drei", channel="remote:device-7")
    await orch.handle_text("vier", channel=VOICE_CHANNEL)
    assert len(set(begun)) == 3
    assert begun[0] == begun[3]
    # The warm model session is bound to the conversation, not the process.
    sessions = [request.metadata["session_id"] for request in router.requests]
    assert sessions == begun
    assert orch.session_id not in sessions
    await orch.stop()


async def test_history_is_replayed_only_within_its_conversation() -> None:
    orch, _, router, _, _ = make(history_turns=8)
    await orch.start()
    await orch.handle_text("geheim am Telefon", channel="remote:device-7")
    await orch.handle_text("hallo vom Desktop", channel="chat:dashboard-1")
    replayed = [m.content for m in router.requests[1].messages if m.role != "system"]
    assert replayed == ["hallo vom Desktop"]
    await orch.stop()


async def test_the_users_words_reach_the_engine_for_each_turn() -> None:
    orch, _, _, _, _ = make()
    taint, begun = _with_tools(orch)
    await orch.start()
    await orch.handle_text("Wie wird das Wetter?", channel="chat:dashboard-1")
    assert taint.user[begun[0]] == "Wie wird das Wetter?"
    await orch.stop()


async def test_a_new_conversation_clears_the_taint_of_the_old_one_only() -> None:
    orch, _, router, _, _ = make()
    taint, begun = _with_tools(orch)
    await orch.start()
    await orch.handle_text("eins", channel="chat:dashboard-1")
    await orch.handle_text("eins", channel=VOICE_CHANNEL)
    chat, voice = begun
    taint.tainted |= {chat, voice}
    asked = len(router.requests)
    turn = await orch.handle_text("Neues Gespräch!", channel="chat:dashboard-1")
    assert turn.fast_path == "new_conversation"
    assert len(router.requests) == asked  # answered by Nox itself
    assert taint.cleared == [chat]
    assert voice in taint.tainted
    await orch.handle_text("zwei", channel="chat:dashboard-1")
    assert begun[-1] != chat  # the next turn runs in a fresh conversation (history, session)
    replayed = [m.content for m in router.requests[-1].messages if m.role != "system"]
    assert "eins" not in replayed
    await orch.stop()


async def test_an_idle_conversation_starts_fresh_and_its_taint_goes() -> None:
    orch, _, _, _, _ = make(conversation_idle_s=60.0)
    now = [1000.0]
    orch.clock = lambda: now[0]
    taint, begun = _with_tools(orch)
    await orch.start()
    await orch.handle_text("eins", channel=VOICE_CHANNEL)
    now[0] += 59.0
    await orch.handle_text("zwei", channel=VOICE_CHANNEL)
    assert begun[0] == begun[1] and taint.cleared == []
    now[0] += 61.0
    await orch.handle_text("drei", channel=VOICE_CHANNEL)
    assert begun[2] != begun[1]
    assert taint.cleared == [begun[1]]
    await orch.stop()


async def test_a_new_turn_alone_never_clears_the_taint() -> None:
    orch, _, _, _, _ = make()
    taint, begun = _with_tools(orch)
    await orch.start()
    for text in ("eins", "zwei", "drei"):
        await orch.handle_text(text, channel=VOICE_CHANNEL)
    assert len(set(begun)) == 1
    assert taint.cleared == []
    await orch.stop()


def test_only_an_explicit_request_starts_over() -> None:
    for text in ("neues Gespräch", "Nox, neues Gespräch bitte", "new conversation", "Neuer Chat."):
        assert is_new_conversation_request(text), text
    for text in ("Erzähl mir von dem neuen Gespräch mit Tim", "kein neues Gespräch", "neu"):
        assert not is_new_conversation_request(text), text


async def test_the_providers_own_web_search_taints_the_conversation_at_once() -> None:
    # Claude Code's WebSearch runs at Anthropic: its results are in the model's context but never
    # pass through Nox. The conversation is tainted the moment the provider reports it.
    orch, _, router, _, _ = make()
    taint, begun = _with_tools(orch)
    plain_stream = router.stream

    seen_during_stream: list[set[str]] = []

    async def searching(request: AiRequest) -> AsyncIterator[AiChunk]:
        yield AiChunk(request_id=request.request_id, delta="", tool_activity=["WebSearch"])
        seen_during_stream.append(set(taint.tainted))
        async for chunk in plain_stream(request):
            yield chunk

    router.stream = searching  # type: ignore[method-assign]
    await orch.start()
    await orch.handle_text("Was gibt es Neues?", channel="chat:dashboard-1")
    assert taint.marks == [(begun[0], None)]  # unreadable: nothing is user-directed after it
    assert seen_during_stream == [{begun[0]}]  # before the stream went on, not after it
    await orch.stop()

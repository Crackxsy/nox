"""nox.stream.responder.StreamResponder: addressed-vs-relevance gating, per-channel and per-viewer
cooldown, max-replies-per-minute (all counted per attempt, also when the send is refused), the
one-line cap, safe mode (kill switch), the tool-call send path and the `untrusted()` wrapper around
chat content."""

from __future__ import annotations

from typing import Any

import pytest

from nox.ai.base import AiRequest, AiResponse, ProviderInfo
from nox.core.config import RelevanceConfig
from nox.core.events import Event
from nox.stream.responder import MAX_REPLY_CHARS, MAX_TRACKED_VIEWERS, StreamResponder
from tests.unit.fakes import FakeBus
from tests.unit.stream.conftest import MutableClock


class FakeCompleteRouter:
    """Only implements `complete()` (non-streaming) - the responder must never call `stream()`."""

    def __init__(self, text: str = "Klar, mach ich!") -> None:
        self.text = text
        self.requests: list[AiRequest] = []

    async def complete(self, request: AiRequest) -> AiResponse:
        self.requests.append(request)
        return AiResponse(
            request_id=request.request_id, provider="fake", text=self.text, latency_ms=1
        )

    def stream(self, request: AiRequest):  # pragma: no cover - must never be called
        raise AssertionError("StreamResponder must use complete(), not stream()")

    async def providers(self) -> list[ProviderInfo]:
        return []

    def explain(self, request_id: str) -> str:
        return "fake"


class FakeExecutor:
    def __init__(self, *, ok: bool = True) -> None:
        self.calls: list[dict[str, Any]] = []
        self._ok = ok

    async def call(self, agent: str, name: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
        self.calls.append({"agent": agent, "name": name, "arguments": arguments, **kwargs})
        return type("Result", (), {"ok": self._ok, "error": None if self._ok else "denied"})()


class FakeKillSwitch:
    def __init__(self, engaged: bool = False) -> None:
        self.engaged = engaged

    def is_engaged(self) -> bool:
        return self.engaged


def make_responder(
    bus: FakeBus,
    router: FakeCompleteRouter,
    executor: FakeExecutor,
    config: RelevanceConfig,
    killswitch: FakeKillSwitch,
    clock: MutableClock,
) -> StreamResponder:
    responder = StreamResponder(bus, router, executor, config, killswitch, clock=clock)
    responder.start()
    return responder


async def _message(
    bus: FakeBus,
    *,
    text: str,
    addressed: bool = False,
    relevance: float = 0.0,
    channel: str = "public",
    viewer: str = "v1",
) -> None:
    await bus.publish(
        Event(
            name="twitch.chat_message",
            payload={
                "chat_event_id": 1,
                "viewer_id": viewer,
                "text": text,
                "addressed_to_nox": addressed,
                "relevance": relevance,
                "channel": channel,
            },
        )
    )


async def test_addressed_message_triggers_an_answer(
    bus: FakeBus, relevance_config: RelevanceConfig, clock: MutableClock
) -> None:
    router = FakeCompleteRouter()
    executor = FakeExecutor()
    make_responder(bus, router, executor, relevance_config, FakeKillSwitch(), clock)
    await _message(bus, text="hey nox, wie gehts?", addressed=True)
    assert len(executor.calls) == 1
    call = executor.calls[0]
    assert call["name"] == "twitch.chat.send"
    assert call["agent"] == "nox.stream"
    assert call["mode"] == "stream"
    assert call["arguments"]["text"] == "Klar, mach ich!"
    assert len(router.requests) == 1
    request = router.requests[0]
    assert request.stream is False
    user_message = next(m for m in request.messages if m.role == "user")
    assert "[[DATA" in user_message.content and "twitch:v1" in user_message.content
    assert "hey nox, wie gehts?" in user_message.content


async def test_below_threshold_and_not_addressed_is_ignored(
    bus: FakeBus, relevance_config: RelevanceConfig, clock: MutableClock
) -> None:
    router = FakeCompleteRouter()
    executor = FakeExecutor()
    make_responder(bus, router, executor, relevance_config, FakeKillSwitch(), clock)
    await _message(bus, text="lol", addressed=False, relevance=relevance_config.threshold - 0.1)
    assert executor.calls == []
    assert router.requests == []


async def test_relevance_at_or_above_threshold_triggers_an_answer(
    bus: FakeBus, relevance_config: RelevanceConfig, clock: MutableClock
) -> None:
    router = FakeCompleteRouter()
    executor = FakeExecutor()
    make_responder(bus, router, executor, relevance_config, FakeKillSwitch(), clock)
    await _message(bus, text="crazy play!", addressed=False, relevance=relevance_config.threshold)
    assert len(executor.calls) == 1


async def test_channel_cooldown_blocks_a_second_reply(
    bus: FakeBus, relevance_config: RelevanceConfig, clock: MutableClock
) -> None:
    router = FakeCompleteRouter()
    executor = FakeExecutor()
    make_responder(bus, router, executor, relevance_config, FakeKillSwitch(), clock)
    await _message(bus, text="hi nox", addressed=True, viewer="v1")
    assert len(executor.calls) == 1
    clock.advance(1.0)  # well inside channel_cooldown_s; another viewer, so only the channel counts
    await _message(bus, text="hi again", addressed=True, viewer="v2")
    assert len(executor.calls) == 1
    clock.advance(relevance_config.channel_cooldown_s)
    await _message(bus, text="hi once more", addressed=True, viewer="v3")
    assert len(executor.calls) == 2


async def test_max_replies_per_minute_is_enforced(bus: FakeBus, clock: MutableClock) -> None:
    config = RelevanceConfig(
        channel_cooldown_s=0.0, viewer_cooldown_s=0.0, max_replies_per_minute=2
    )
    router = FakeCompleteRouter()
    executor = FakeExecutor()
    make_responder(bus, router, executor, config, FakeKillSwitch(), clock)
    for i in range(3):
        await _message(bus, text=f"msg {i}", addressed=True)
        clock.advance(1.0)
    assert len(executor.calls) == 2
    clock.advance(60.0)  # the window has moved on
    await _message(bus, text="later", addressed=True)
    assert len(executor.calls) == 3


async def test_safe_mode_never_answers(
    bus: FakeBus, relevance_config: RelevanceConfig, clock: MutableClock
) -> None:
    router = FakeCompleteRouter()
    executor = FakeExecutor()
    make_responder(bus, router, executor, relevance_config, FakeKillSwitch(engaged=True), clock)
    await _message(bus, text="hey nox", addressed=True)
    assert executor.calls == []
    assert router.requests == []


async def test_answer_is_trimmed_to_one_line_under_400_chars(
    bus: FakeBus, relevance_config: RelevanceConfig, clock: MutableClock
) -> None:
    long_text = "a" * 500
    router = FakeCompleteRouter(text=f"line one\nline two {long_text}")
    executor = FakeExecutor()
    make_responder(bus, router, executor, relevance_config, FakeKillSwitch(), clock)
    await _message(bus, text="tell me something", addressed=True)
    sent = executor.calls[0]["arguments"]["text"]
    assert "\n" not in sent
    assert sent == "line one"
    assert len(sent) <= 400


async def test_a_long_single_line_is_cut_to_exactly_the_cap(
    bus: FakeBus, relevance_config: RelevanceConfig, clock: MutableClock
) -> None:
    assert MAX_REPLY_CHARS == 400
    router = FakeCompleteRouter(text="wort " * 200)
    executor = FakeExecutor()
    make_responder(bus, router, executor, relevance_config, FakeKillSwitch(), clock)
    await _message(bus, text="tell me something", addressed=True)
    sent = executor.calls[0]["arguments"]["text"]
    assert len(sent) == MAX_REPLY_CHARS
    assert sent.endswith("wort" + ELLIPSIS)


ELLIPSIS = chr(0x2026)


@pytest.mark.parametrize(("length", "expected"), [(399, 399), (400, 400), (401, 400), (5000, 400)])
async def test_the_cap_holds_at_its_exact_value(
    bus: FakeBus,
    relevance_config: RelevanceConfig,
    clock: MutableClock,
    length: int,
    expected: int,
) -> None:
    router = FakeCompleteRouter(text="x" * length)
    executor = FakeExecutor()
    make_responder(bus, router, executor, relevance_config, FakeKillSwitch(), clock)
    await _message(bus, text="tell me something", addressed=True)
    sent = executor.calls[0]["arguments"]["text"]
    assert len(sent) == expected
    assert sent.endswith(ELLIPSIS) is (length > MAX_REPLY_CHARS)


async def test_a_refused_send_still_uses_up_the_channel_cooldown(
    bus: FakeBus, relevance_config: RelevanceConfig, clock: MutableClock
) -> None:
    """The model call is what costs: a line the plugin refuses must not make the next message a
    free one, or a chatter whose answers are always refused keeps the model busy for ever."""
    router = FakeCompleteRouter()
    executor = FakeExecutor(ok=False)
    make_responder(bus, router, executor, relevance_config, FakeKillSwitch(), clock)
    await _message(bus, text="hi nox", addressed=True, viewer="v1")
    clock.advance(relevance_config.channel_cooldown_s - 1)
    await _message(bus, text="hi nox again", addressed=True, viewer="v2")
    assert len(router.requests) == 1
    assert len(executor.calls) == 1
    clock.advance(1.0)  # the cooldown is over: the channel is open again
    await _message(bus, text="and again", addressed=True, viewer="v3")
    assert len(router.requests) == 2


async def test_a_refused_send_still_uses_up_the_viewer_cooldown(
    bus: FakeBus, clock: MutableClock
) -> None:
    config = RelevanceConfig(channel_cooldown_s=0.0, viewer_cooldown_s=30.0)
    router = FakeCompleteRouter()
    executor = FakeExecutor(ok=False)
    make_responder(bus, router, executor, config, FakeKillSwitch(), clock)
    await _message(bus, text="hi nox", addressed=True, viewer="v1")
    clock.advance(29.0)
    await _message(bus, text="hi nox again", addressed=True, viewer="v1")
    assert len(router.requests) == 1
    clock.advance(1.0)
    await _message(bus, text="hi nox once more", addressed=True, viewer="v1")
    assert len(router.requests) == 2


async def test_a_refused_send_still_counts_toward_the_replies_per_minute(
    bus: FakeBus, clock: MutableClock
) -> None:
    config = RelevanceConfig(
        channel_cooldown_s=0.0, viewer_cooldown_s=0.0, max_replies_per_minute=3
    )
    router = FakeCompleteRouter()
    make_responder(bus, router, FakeExecutor(ok=False), config, FakeKillSwitch(), clock)
    for i in range(6):
        await _message(bus, text=f"msg {i}", addressed=True, viewer=f"v{i}")
        clock.advance(1.0)
    assert len(router.requests) == 3


async def test_an_empty_answer_still_uses_up_the_cooldowns(
    bus: FakeBus, relevance_config: RelevanceConfig, clock: MutableClock
) -> None:
    router = FakeCompleteRouter(text="   ")
    executor = FakeExecutor()
    make_responder(bus, router, executor, relevance_config, FakeKillSwitch(), clock)
    await _message(bus, text="hi nox", addressed=True, viewer="v1")
    clock.advance(1.0)
    await _message(bus, text="hi nox again", addressed=True, viewer="v2")
    assert len(router.requests) == 1
    assert executor.calls == []


async def test_a_second_message_while_the_model_is_busy_is_throttled(
    bus: FakeBus, relevance_config: RelevanceConfig, clock: MutableClock
) -> None:
    """The attempt is counted before the model is asked, not when its answer comes back."""

    class BusyRouter(FakeCompleteRouter):
        async def complete(self, request: AiRequest) -> AiResponse:
            await _message(bus, text="hi nox, during the call", addressed=True, viewer="v2")
            return await super().complete(request)

    router = BusyRouter()
    make_responder(bus, router, FakeExecutor(), relevance_config, FakeKillSwitch(), clock)
    await _message(bus, text="hi nox", addressed=True, viewer="v1")
    assert len(router.requests) == 1


async def test_the_viewer_cooldown_is_per_viewer(bus: FakeBus, clock: MutableClock) -> None:
    config = RelevanceConfig(channel_cooldown_s=0.0, viewer_cooldown_s=30.0)
    router = FakeCompleteRouter()
    make_responder(bus, router, FakeExecutor(), config, FakeKillSwitch(), clock)
    await _message(bus, text="hi nox", addressed=True, viewer="v1")
    await _message(bus, text="hi nox", addressed=True, viewer="v2")  # another viewer is not held
    await _message(bus, text="hi nox", addressed=True, viewer="v1")  # the same one is
    assert len(router.requests) == 2


async def test_the_viewer_table_forgets_viewers_whose_cooldown_is_over(
    bus: FakeBus, clock: MutableClock
) -> None:
    config = RelevanceConfig(
        channel_cooldown_s=0.0, viewer_cooldown_s=10.0, max_replies_per_minute=100_000
    )
    responder = make_responder(
        bus, FakeCompleteRouter(), FakeExecutor(), config, FakeKillSwitch(), clock
    )
    for i in range(MAX_TRACKED_VIEWERS + 1):
        await _message(bus, text="hi nox", addressed=True, viewer=f"v{i}")
    assert len(responder._last_reply_by_viewer) == MAX_TRACKED_VIEWERS + 1  # all still held
    clock.advance(11.0)
    await _message(bus, text="hi nox", addressed=True, viewer="late")
    assert set(responder._last_reply_by_viewer) == {"late"}  # the others were over


# -- commands ------------------------------------------------------------------------------------


async def test_a_command_is_not_answered_even_with_a_high_relevance_score(
    bus: FakeBus, relevance_config: RelevanceConfig, clock: MutableClock
) -> None:
    """The responder does not trust the score for a `!` line: "!lurk why?" must stay silent even
    if some other producer scored it like a question."""
    router = FakeCompleteRouter()
    executor = FakeExecutor()
    make_responder(bus, router, executor, relevance_config, FakeKillSwitch(), clock)
    await _message(bus, text="!lurk why?", addressed=False, relevance=1.0)
    assert router.requests == []
    assert executor.calls == []


async def test_a_command_marked_as_addressed_is_answered(
    bus: FakeBus, relevance_config: RelevanceConfig, clock: MutableClock
) -> None:
    router = FakeCompleteRouter()
    executor = FakeExecutor()
    make_responder(bus, router, executor, relevance_config, FakeKillSwitch(), clock)
    await _message(bus, text="!nox hallo", addressed=True, relevance=1.0)
    assert len(executor.calls) == 1

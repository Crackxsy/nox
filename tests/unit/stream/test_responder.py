"""nox.stream.responder.StreamResponder: addressed-vs-relevance gating, per-channel cooldown,
max-replies-per-minute, safe mode (kill switch), the tool-call send path and the `untrusted()`
wrapper around chat content."""

from __future__ import annotations

from typing import Any

from nox.ai.base import AiRequest, AiResponse, ProviderInfo
from nox.core.config import RelevanceConfig
from nox.core.events import Event
from nox.stream.responder import StreamResponder
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
) -> None:
    await bus.publish(
        Event(
            name="twitch.chat_message",
            payload={
                "chat_event_id": 1,
                "viewer_id": "v1",
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
    await _message(bus, text="hi nox", addressed=True)
    assert len(executor.calls) == 1
    clock.advance(1.0)  # well inside channel_cooldown_s
    await _message(bus, text="hi again", addressed=True)
    assert len(executor.calls) == 1
    clock.advance(relevance_config.channel_cooldown_s)
    await _message(bus, text="hi once more", addressed=True)
    assert len(executor.calls) == 2


async def test_max_replies_per_minute_is_enforced(bus: FakeBus, clock: MutableClock) -> None:
    config = RelevanceConfig(channel_cooldown_s=0.0, max_replies_per_minute=2)
    router = FakeCompleteRouter()
    executor = FakeExecutor()
    make_responder(bus, router, executor, config, FakeKillSwitch(), clock)
    for i in range(3):
        await _message(bus, text=f"msg {i}", addressed=True)
        clock.advance(1.0)
    assert len(executor.calls) == 2


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


async def test_unsuccessful_send_does_not_count_toward_cooldown(
    bus: FakeBus, relevance_config: RelevanceConfig, clock: MutableClock
) -> None:
    router = FakeCompleteRouter()
    executor = FakeExecutor(ok=False)
    make_responder(bus, router, executor, relevance_config, FakeKillSwitch(), clock)
    await _message(bus, text="hi nox", addressed=True)
    assert len(executor.calls) == 1
    clock.advance(0.5)
    await _message(bus, text="hi nox again", addressed=True)
    # denied sends never registered a cooldown, so a second attempt goes through too
    assert len(executor.calls) == 2

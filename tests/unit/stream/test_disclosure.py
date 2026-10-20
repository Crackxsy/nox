"""The AI-voice disclosure: one chat line per stream session through the ToolExecutor, the built-in
text in Nox's language, failures that never escape, and the overlay label in the state."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from nox.core.config import NoxConfig, StreamAiVoiceDisclosureConfig
from nox.core.events import E
from nox.core.state import NoxState
from nox.stream.disclosure import (
    LABEL_PATH,
    AiVoiceDisclosure,
    ai_voice_label,
    default_chat_text,
    disclosure_text,
)
from tests.unit.fakes import FakeBus


class FakeExecutor:
    def __init__(self, results: list[Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.results = results or []

    async def call(self, agent: str, name: str, arguments: dict[str, Any], *, mode: str) -> Any:
        self.calls.append({"agent": agent, "name": name, "arguments": arguments, "mode": mode})
        result = self.results.pop(0) if self.results else SimpleNamespace(ok=True, error=None)
        if isinstance(result, Exception):
            raise result
        return result


class FakeState:
    def __init__(self) -> None:
        self.updates: list[tuple[str, Any]] = []

    async def update(self, path: str, value: Any, *, reason: str = "") -> int:
        self.updates.append((path, value))
        return len(self.updates)


def _service(
    executor: FakeExecutor, text: str = "Hinweis", state: Any = None, label: str = "KI-Stimme"
) -> tuple[FakeBus, AiVoiceDisclosure]:
    bus = FakeBus()
    service = AiVoiceDisclosure(bus, executor, text=lambda: text, label=lambda: label, state=state)
    service.start()
    return bus, service


async def test_posted_once_per_session_through_the_executor() -> None:
    executor = FakeExecutor()
    bus, _ = _service(executor)
    await bus.fire(E.STREAM_STARTED, {"session_id": "s1", "mode": "live"})
    await bus.fire(E.STREAM_STARTED, {"session_id": "s1b", "mode": "live"})  # a reconnect
    await bus.fire(E.TWITCH_CONNECTED, {})
    assert executor.calls == [
        {
            "agent": "nox.stream",
            "name": "twitch.chat.send",
            "arguments": {"text": "Hinweis"},
            "mode": "stream",
        }
    ]
    await bus.fire(E.STREAM_ENDED, {"session_id": "s1", "duration_s": 5.0})
    await bus.fire(E.STREAM_STARTED, {"session_id": "s2", "mode": "live"})
    assert len(executor.calls) == 2  # a new session says it again


async def test_disabled_posts_nothing() -> None:
    executor = FakeExecutor()
    bus, _ = _service(executor, text="")
    await bus.fire(E.STREAM_STARTED, {"session_id": "s1", "mode": "live"})
    await bus.fire(E.TWITCH_CONNECTED, {})
    assert executor.calls == []


async def test_a_failed_send_is_logged_and_retried_once_when_the_chat_connects() -> None:
    executor = FakeExecutor(
        [SimpleNamespace(ok=False, error="plugin not running"), RuntimeError("boom")]
    )
    bus, _ = _service(executor)
    await bus.fire(E.STREAM_STARTED, {"session_id": "s1", "mode": "live"})  # no raise
    await bus.fire(E.TWITCH_CONNECTED, {})  # retry, raises inside: swallowed
    await bus.fire(E.TWITCH_CONNECTED, {})  # attempts used up
    assert len(executor.calls) == 2


async def test_a_successful_retry_ends_the_retries() -> None:
    executor = FakeExecutor([SimpleNamespace(ok=False, error="not connected")])
    bus, _ = _service(executor)
    await bus.fire(E.STREAM_STARTED, {"session_id": "s1", "mode": "live"})
    await bus.fire(E.TWITCH_CONNECTED, {})
    await bus.fire(E.TWITCH_CONNECTED, {})
    assert len(executor.calls) == 2


async def test_nothing_is_sent_outside_a_stream() -> None:
    executor = FakeExecutor()
    bus, service = _service(executor)
    await bus.fire(E.TWITCH_CONNECTED, {})
    service.stop()
    await bus.fire(E.STREAM_STARTED, {"session_id": "s1", "mode": "live"})
    assert executor.calls == []


def _config(**identity: Any) -> NoxConfig:
    return NoxConfig.model_validate({"identity": identity} if identity else {})


def test_the_default_text_follows_the_speech_language_and_names_nox() -> None:
    assert disclosure_text(_config(name="Kumo", speech_language="de")) == (
        "Kurzer Hinweis: Die Stimme von Kumo ist KI-generiert."
    )
    assert disclosure_text(_config(name="Kumo", speech_language="en")) == (
        "Quick note: Kumo's voice is AI-generated."
    )
    # `auto` speech language: the interface language decides.
    assert "AI-generated" in disclosure_text(_config(speech_language="auto", ui_language="en"))
    assert default_chat_text("  ", "de").endswith("von Nox ist KI-generiert.")


def test_a_custom_text_wins_and_disabled_is_empty() -> None:
    config = NoxConfig.model_validate(
        {"stream": {"ai_voice_disclosure": {"chat_text": "  Synthetic voice!  "}}}
    )
    assert disclosure_text(config) == "Synthetic voice!"
    off = NoxConfig.model_validate({"stream": {"ai_voice_disclosure": {"enabled": False}}})
    assert disclosure_text(off) == ""


def test_the_overlay_label_defaults_on_and_can_be_switched_off() -> None:
    assert ai_voice_label(StreamAiVoiceDisclosureConfig()) == "KI-Stimme"
    assert ai_voice_label(StreamAiVoiceDisclosureConfig(overlay_label=" ")) == ""
    assert ai_voice_label(StreamAiVoiceDisclosureConfig(enabled=False)) == ""
    assert NoxConfig().stream.ai_voice_disclosure.enabled is True


async def test_the_label_lands_in_the_public_system_state() -> None:
    state = FakeState()
    _, service = _service(FakeExecutor(), state=state, label="AI voice")
    await service.publish_label()
    assert state.updates == [(LABEL_PATH, "AI voice")]
    assert LABEL_PATH == "system.ai_voice_label"
    assert NoxState().system.ai_voice_label == ""


async def test_a_state_that_refuses_the_label_does_not_break_the_boot() -> None:
    class Broken:
        async def update(self, path: str, value: Any, *, reason: str = "") -> int:
            raise ValueError("nope")

    _, service = _service(FakeExecutor(), state=Broken())
    await service.publish_label()  # no exception

"""The desktop pet and the stream overlay get an allow-listed view of each event: no spoken or
transcribed text, no private state, only the pet's own settings - and the overlay nothing about
headphone-only speech and nothing at all while privacy is PRIVATE or OFFLINE."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest

from nox.core.events import Event
from nox.ipc.overlay_tokens import OverlayTokenStore
from nox.ipc.pet_view import public_snapshot, view
from nox.ipc.protocol import Kind
from nox.ipc.server import IpcHub
from nox.ipc.tokens import TokenStore
from tests.unit.ipc.conftest import RawClient, SimpleBus

ROLES = ("pet", "overlay")


@pytest.mark.parametrize("role", ROLES)
def test_no_spoken_text(role: str) -> None:
    started = view(
        role,
        "tts.started",
        {"text": "secret plan", "channel": "stream", "engine": "piper", "utterance_id": "u1"},
    )
    assert started == {"utterance_id": "u1", "channel": "stream"}
    chunk = view(role, "tts.chunk", {"text": "secret plan", "utterance_id": "u1"})
    assert chunk == {"utterance_id": "u1", "chars": 11}


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize(
    "name",
    [
        "voice.transcript_ready",
        "voice.transcript_partial",
        "ai.response_chunk",
        "chat.message",
        "memory.saved",
        "obs.scene_changed",
        "system.health_changed",
        "twitch.chat_message",
    ],
)
def test_events_outside_the_list_are_dropped(role: str, name: str) -> None:
    assert view(role, name, {"text": "x"}) is None


@pytest.mark.parametrize("role", ROLES)
def test_no_private_state(role: str) -> None:
    assert view(role, "state.changed", {"path": "assistant.current_thought", "new": "x"}) is None
    assert view(role, "state.changed", {"path": "assistant.current_task", "new": "x"}) is None
    assert view(role, "state.changed", {"path": "memory.last", "new": "x"}) is None
    assert view(role, "state.changed", {"path": "voice.routing", "new": "both"}) is None
    sleep = view(role, "state.changed", {"path": "assistant.sleep", "new": "deep", "version": 3})
    assert sleep == {"path": "assistant.sleep", "new": "deep", "version": 3}


@pytest.mark.parametrize("role", ROLES)
def test_only_pet_settings(role: str) -> None:
    assert view(role, "settings.changed", {"paths": ["voice.channels.stream_device"]}) is None
    changed = view(role, "settings.changed", {"paths": ["pet.variant", "identity.name"]})
    assert changed == {"paths": ["pet.variant"]}


def test_overlay_hears_nothing_of_headphone_only_speech_the_pet_does() -> None:
    private = {"text": "x", "channel": "private", "utterance_id": "u1"}
    assert view("overlay", "tts.started", private) is None
    assert view("pet", "tts.started", private) == {"utterance_id": "u1", "channel": "private"}


@pytest.mark.parametrize("mode", ["private", "offline"])
def test_overlay_gets_nothing_but_the_mode_in_private_or_offline(mode: str) -> None:
    for name, payload in [
        ("pet.state_changed", {"expression": "happy"}),
        ("tts.started", {"channel": "stream", "utterance_id": "u"}),
        ("state.changed", {"path": "assistant.sleep", "new": "none"}),
    ]:
        assert view("overlay", name, payload, privacy_mode=mode) is None
        assert view("pet", name, payload, privacy_mode=mode) is not None
    changed = view("overlay", "privacy.mode_changed", {"current": mode}, privacy_mode=mode)
    assert changed == {"current": mode}


def test_state_get_snapshot_without_words() -> None:
    snapshot = {
        "assistant": {"sleep": "none", "current_thought": "x", "current_task": "y"},
        "privacy": {"mode": "balanced"},
        "system": {"level": "running"},
        "memory": {"secret": True},
    }
    public = public_snapshot(snapshot)
    assert public == {
        "assistant": {"sleep": "none"},
        "privacy": {"mode": "balanced"},
        "system": {"level": "running"},
    }


# ---- through the hub ---------------------------------------------------------------------------

Raw = Callable[..., Awaitable[RawClient]]


@pytest.fixture
def store(tmp_path) -> OverlayTokenStore:  # type: ignore[no-untyped-def]
    return OverlayTokenStore(tmp_path / "overlay.json")


@pytest.fixture
def tokens(store: OverlayTokenStore) -> TokenStore:
    return TokenStore(overlay=store)


async def test_hub_sends_the_overlay_the_reduced_view_and_nothing_in_private_mode(
    hub: IpcHub, raw: Raw, store: OverlayTokenStore, bus: SimpleBus
) -> None:
    _, token = store.issue()
    client = await raw("overlay", "overlay:1")
    assert (await client.auth(token)).kind is Kind.RESPONSE
    await client.request("ipc.subscribe", {"patterns": ["**"]})
    await bus.publish(
        Event(name="tts.chunk", payload={"text": "secret words", "utterance_id": "u"})
    )
    chunk = await client.recv_named("tts.chunk")
    assert chunk.payload == {"utterance_id": "u", "chars": 12}

    hub.privacy_mode = lambda: "private"
    await bus.publish(Event(name="tts.chunk", payload={"text": "more", "utterance_id": "u2"}))
    await bus.publish(Event(name="privacy.mode_changed", payload={"current": "private"}))
    mode = await client.recv(timeout=2.0)
    assert mode.name == "privacy.mode_changed"  # the chunk before it never came

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


SNAPSHOT = {
    "assistant": {"sleep": "none", "muted": False, "current_thought": "x", "current_task": "y"},
    "privacy": {"mode": "balanced", "microphone": True},
    "system": {"level": "running"},
    "voice": {"routing": "both"},
    "memory": {"secret": True},
}


def _leaves(tree: dict, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in tree.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_leaves(value, path + "."))
        else:
            out[path] = value
    return out


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("mode", ["full", "balanced", "private", "offline"])
def test_state_get_and_state_events_agree(role: str, mode: str) -> None:
    """Parity: a path is in the `state.get` answer exactly when its change event is delivered."""
    asked = set(_leaves(public_snapshot(SNAPSHOT, role, mode)))
    told = {
        path
        for path, value in _leaves(SNAPSHOT).items()
        if view(role, "state.changed", {"path": path, "new": value}, privacy_mode=mode) is not None
    }
    assert asked == told


@pytest.mark.parametrize("mode", ["private", "offline"])
def test_quiet_overlay_state_is_the_mode_only(mode: str) -> None:
    assert public_snapshot(SNAPSHOT, "overlay", mode) == {"privacy": {"mode": "balanced"}}
    assert "assistant" in public_snapshot(SNAPSHOT, "pet", mode)


LABELLED = {**SNAPSHOT, "system": {"level": "running", "ai_voice_label": "KI-Stimme"}}


@pytest.mark.parametrize("mode", ["private", "offline"])
def test_the_ai_voice_label_stays_on_the_quiet_overlay_and_nothing_else_joins_it(
    mode: str,
) -> None:
    """The disclosure is a configured constant: it must not vanish with the privacy mode, and it
    must not open a door for the rest of `system`."""
    assert public_snapshot(LABELLED, "overlay", mode) == {
        "privacy": {"mode": "balanced"},
        "system": {"ai_voice_label": "KI-Stimme"},
    }
    changed = {"path": "system.ai_voice_label", "new": "AI voice", "old": "", "version": 3}
    assert view("overlay", "state.changed", changed, privacy_mode=mode) == changed
    level = {"path": "system.level", "new": "running"}
    assert view("overlay", "state.changed", level, privacy_mode=mode) is None


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("mode", ["full", "balanced", "private", "offline"])
def test_state_get_and_state_events_agree_with_the_label(role: str, mode: str) -> None:
    asked = set(_leaves(public_snapshot(LABELLED, role, mode)))
    told = {
        path
        for path, value in _leaves(LABELLED).items()
        if view(role, "state.changed", {"path": path, "new": value}, privacy_mode=mode) is not None
    }
    assert asked == told
    assert "system.ai_voice_label" in asked


# ---- tts.level: the voice's loudness, for the mouth ----------------------------------------------


@pytest.mark.parametrize("role", ROLES)
def test_tts_level_is_one_number(role: str) -> None:
    shown = view(role, "tts.level", {"utterance_id": "u1", "channel": "stream", "level": 0.4567})
    assert shown == {"level": 0.457}
    assert all(isinstance(v, float) for v in shown.values())


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("bad", ["loud", None, True, float("nan"), float("inf"), [0.5]])
def test_tts_level_that_is_not_a_number_is_dropped(role: str, bad: object) -> None:
    payload = {"utterance_id": "u1", "channel": "stream", "level": bad}
    assert view(role, "tts.level", payload) is None


def test_tts_level_is_clamped() -> None:
    assert view("pet", "tts.level", {"channel": "stream", "level": 7}) == {"level": 1.0}
    assert view("pet", "tts.level", {"channel": "stream", "level": -1}) == {"level": 0.0}


def test_overlay_gets_no_level_of_headphone_speech_nor_in_quiet_modes() -> None:
    private = {"utterance_id": "u1", "channel": "private", "level": 0.5}
    assert view("overlay", "tts.level", private) is None
    assert view("pet", "tts.level", private) == {"level": 0.5}
    stream = {"utterance_id": "u1", "channel": "stream", "level": 0.5}
    for mode in ("private", "offline"):
        assert view("overlay", "tts.level", stream, privacy_mode=mode) is None


def test_only_the_pet_and_the_overlay_receive_levels() -> None:
    from nox.ipc.server import role_may_see

    assert role_may_see("pet", "tts.level")
    assert role_may_see("overlay", "tts.level")
    for role in ("dashboard", "shell", "remote", "plugin", "worker"):
        assert not role_may_see(role, "tts.level"), role
    assert role_may_see("dashboard", "tts.started")

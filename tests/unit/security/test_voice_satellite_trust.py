"""C9 review H1/H3: what a node's voice satellite heard is lower-trust input from its first turn,
and a node's speech levels reach only that node's own overlay.

The real permission engine and its `TaintLedger`; fictitious names only ("Living room Pi").
"""

from __future__ import annotations

from datetime import timedelta

from nox.ipc import pet_view
from nox.security.model import Decision, Risk, TemporaryGrant
from nox.security.permissions import DefaultPermissionEngine, InMemoryGrantStore
from nox.shell.logic import voice_satellite_line

from .conftest import MutableClock, req

PI = "a" * 32
OTHER = "b" * 32
VOICE = f"voice:node:{PI}#c0ffee"
CHAT = "chat:dashboard:1#c0ffee"


def _grant(clock: MutableClock, risk: Risk) -> TemporaryGrant:
    return TemporaryGrant(
        grant_id=f"g-{risk.value}",
        scope="session:notes/*",
        max_risk=risk,
        expires_at=clock.now + timedelta(hours=1),
    )


def test_a_voice_satellite_conversation_is_tainted_from_its_first_turn(
    engine: DefaultPermissionEngine,
) -> None:
    assert engine.is_tainted(VOICE)  # nothing was marked: the source alone decides
    assert not engine.is_tainted(CHAT)
    engine.clear_taint(VOICE)  # a discarded context does not make it the owner's words
    assert engine.is_tainted(VOICE)


def test_every_call_that_sends_data_out_asks(engine: DefaultPermissionEngine) -> None:
    upload = req("notes", "sync", risk=Risk.LOW, network=True)
    assert engine.check(upload.model_copy(update={"conversation": CHAT})).decision is (
        Decision.ALLOW
    )
    assert engine.check(upload.model_copy(update={"conversation": VOICE})).decision is (
        Decision.CONFIRM
    )


def test_no_remembered_grant_above_low_applies(
    engine: DefaultPermissionEngine, grants: InMemoryGrantStore, clock: MutableClock
) -> None:
    grants.add(_grant(clock, Risk.MEDIUM))
    write = req("notes", "write", risk=Risk.MEDIUM, target="notes/a.md")
    assert engine.check(write.model_copy(update={"conversation": CHAT})).decision is (
        Decision.ALLOW
    )
    decision = engine.check(write.model_copy(update={"conversation": VOICE})).decision
    assert decision is not Decision.ALLOW


def test_nothing_spoken_there_counts_as_user_directed(engine: DefaultPermissionEngine) -> None:
    engine.note_user_message(VOICE, "wetter in springfield")
    search = req(
        "web",
        "search",
        risk=Risk.LOW,
        network=True,
        conversation=VOICE,
        lookup="wetter springfield",
    )
    assert engine.check(search).decision is Decision.CONFIRM
    engine.note_user_message(CHAT, "wetter in springfield")
    engine.mark_tainted(CHAT, "some page")
    clean = search.model_copy(update={"conversation": CHAT})
    assert engine.check(clean).decision is Decision.ALLOW  # the owner's own words, typed


async def test_the_question_says_which_satellite_the_words_came_from(
    engine: DefaultPermissionEngine,
) -> None:
    engine.satellite_label = lambda device_id: "Living room Pi" if device_id == PI else ""
    engine.request_confirmation(req("notes", "write", risk=Risk.MEDIUM, conversation=VOICE))
    engine.request_confirmation(req("notes", "write", risk=Risk.MEDIUM, conversation=CHAT))
    shown = [p.voice_satellite for p in engine.pending_requests()]
    assert shown == ["Living room Pi", ""]
    line = voice_satellite_line({"voice_satellite": "Living room Pi"}, "de")
    assert line.startswith("Von Sprach-Satellit Living room Pi")
    assert voice_satellite_line({"voice_satellite": "Living room Pi"}, "en").startswith(
        "From voice satellite Living room Pi"
    )
    assert voice_satellite_line({}, "de") == ""


# ---- H3: speech levels ---------------------------------------------------------------------------


def _level(device: str = "", channel: str = "node") -> dict[str, object]:
    payload: dict[str, object] = {"utterance_id": "u", "channel": channel, "level": 0.42}
    if device:
        payload["device"] = device
    return payload


def test_a_nodes_speech_reaches_only_that_nodes_own_overlay() -> None:
    node_level = _level(PI)
    assert pet_view.view("overlay", "tts.level", node_level, overlay_device=PI) == {
        "level": 0.42,
        "device": PI,
    }
    assert pet_view.view("overlay", "tts.level", node_level, overlay_device=OTHER) is None
    assert pet_view.view("overlay", "tts.level", node_level) is None  # the core's OBS overlay
    assert pet_view.view("pet", "tts.level", node_level) is None  # the core's desktop pet
    # Quiet privacy modes say nothing of it either.
    assert (
        pet_view.view("overlay", "tts.level", node_level, privacy_mode="private", overlay_device=PI)
        is None
    )
    # A malformed device is no device of anyone's.
    assert pet_view.view("overlay", "tts.level", _level("x"), overlay_device=PI) is None


def test_the_cores_own_speech_is_unchanged() -> None:
    core = {**_level(channel="both"), "device": ""}  # the core's own event model sends ""
    assert pet_view.view("pet", "tts.level", core) == {"level": 0.42}
    assert pet_view.view("overlay", "tts.level", core) == {"level": 0.42}
    private = {**_level(channel="private"), "device": ""}
    assert pet_view.view("overlay", "tts.level", private) is None
    assert pet_view.view("pet", "tts.level", private) == {"level": 0.42}


def test_even_with_a_tool_result_and_the_users_words_nothing_is_user_directed(
    engine: DefaultPermissionEngine,
) -> None:
    engine.mark_tainted(VOICE, "a page about the weather")  # readable, not opaque
    engine.note_user_message(VOICE, "wetter in springfield")
    search = req(
        "web",
        "search",
        risk=Risk.LOW,
        network=True,
        conversation=VOICE,
        lookup="wetter springfield",
    )
    assert engine.check(search).decision is Decision.CONFIRM

"""Event payload round-trips for the Stream Bot additions (Spec v0.2 §8): every new name in `E`
has a registered model in `PAYLOAD_MODELS` and validates a representative payload."""

from __future__ import annotations

from typing import Any

import pytest

from nox.core.events import PAYLOAD_MODELS, E, validate_payload

STREAM_BOT_EVENTS: dict[str, dict[str, Any]] = {
    E.STREAM_STARTED: {
        "session_id": "s1",
        "mode": "live",
        "obs_connected": True,
        "twitch_connected": True,
    },
    E.STREAM_ENDED: {"session_id": "s1", "duration_s": 120.0, "ended_reason": "manual"},
    E.STREAM_MODE_CHANGED: {"previous": "idle", "current": "live", "by": "manual"},
    E.STREAM_PREFLIGHT_RESULT: {
        "session_id": "s1",
        "items": [{"name": "obs", "status": "green", "detail": ""}],
        "overall": "green",
    },
    E.STREAM_VIEWER_SEEN: {"viewer_id": "v1", "first_time": True},
    E.STREAM_FUNKEN_AWARDED: {"viewer_id": "v1", "delta": 1.0, "balance_after": 5.0},
    E.STREAM_FUNKEN_CHANGED: {
        "viewer_id": "v1",
        "delta": 1.0,
        "balance_after": 5.0,
        "source": "earn",
    },
    E.STREAM_MINIGAME_STARTED: {"session_id": "s1", "game_id": "rps", "participants": ["v1"]},
    E.STREAM_MINIGAME_ENDED: {
        "session_id": "s1",
        "game_id": "rps",
        "participants": ["v1"],
        "result": {"winner": "v1"},
    },
    E.OBS_CONNECTED: {"reason": ""},
    E.OBS_DISCONNECTED: {"reason": "closed", "backoff_s": 1.0},
    E.OBS_SCENE_CHANGED: {"previous_scene": "Start", "current_scene": "Live", "by": "nox"},
    E.OBS_HEALTH_CHANGED: {
        "mic_level_ok": True,
        "camera_ok": True,
        "render_lag_pct": 0.0,
        "dropped_frames_pct": 0.0,
    },
    E.OBS_CRASH_DETECTED: {},
    E.OBS_AUTO_RESTARTED: {"attempt": 1, "succeeded": True},
    E.TWITCH_CONNECTED: {"reason": ""},
    E.TWITCH_DISCONNECTED: {"reason": "closed", "backoff_s": 2.0},
    E.TWITCH_RESYNCED: {"messages_recovered": 3, "messages_lost_estimate": 0, "gap_s": 1.5},
    E.TWITCH_CHAT_MESSAGE: {
        "chat_event_id": 1,
        "viewer_id": "v1",
        "text": "hi",
        "channel": "public",
    },
    E.TWITCH_COMMAND_INVOKED: {
        "chat_event_id": 1,
        "viewer_id": "v1",
        "command": "points",
        "args": [],
    },
    E.TWITCH_EVENT: {"chat_event_id": 1, "kind": "sub", "viewer_id": "v1", "priority": 1},
    E.TWITCH_CHAT_MOOD_CHANGED: {"category": "hype", "window": "short"},
    E.TWITCH_MODERATION_ACTION: {
        "chat_event_id": 1,
        "viewer_id": "v1",
        "stage": "ignore",
        "hard_list_hit": False,
    },
}


def test_every_new_event_name_is_registered() -> None:
    for name in STREAM_BOT_EVENTS:
        assert name in PAYLOAD_MODELS, f"{name} has no payload model registered"


@pytest.mark.parametrize("name", list(STREAM_BOT_EVENTS))
def test_round_trip(name: str) -> None:
    payload = STREAM_BOT_EVENTS[name]
    validated = validate_payload(name, payload)
    # every field we passed survives validation unchanged
    for key, value in payload.items():
        assert validated[key] == value


def test_twitch_chat_message_defaults_to_public_channel() -> None:
    validated = validate_payload(
        E.TWITCH_CHAT_MESSAGE, {"chat_event_id": 1, "viewer_id": "v1", "text": "hi"}
    )
    assert validated["channel"] == "public"

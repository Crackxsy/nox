"""Event payload round-trips for the Clip Pipeline additions (Spec v0.6 §8, EPIC-15): every new
`clip.*` name in `E` has a registered model in `PAYLOAD_MODELS` and validates a representative
payload (Event Model testing rule, same pattern as `tests/unit/core/test_events.py`)."""

from __future__ import annotations

from typing import Any

import pytest

from nox.core.events import PAYLOAD_MODELS, E, validate_payload

CLIP_EVENTS: dict[str, dict[str, Any]] = {
    E.CLIP_REQUESTED: {
        "trigger_kind": "rl.goal",
        "source": "event",
        "origin_event_id": "42",
        "session_id": "s1",
        "tags": ["goal"],
    },
    E.CLIP_SAVED: {
        "clip_id": "c1",
        "file_path": r"E:\Nox\data\clips\library\c1.mp4",
        "trigger_kind": "rl.goal",
        "source": "event",
        "duration_s": 12.5,
        "tags": ["goal"],
    },
    E.CLIP_FAILED: {
        "trigger_kind": "rl.goal",
        "source": "event",
        "reason": "OBS's replay buffer is not enabled/active",
    },
    E.CLIP_EXPORTED: {"clip_id": "c1", "export_path": r"E:\Nox\data\clips\export\c1.mp4"},
}


def test_every_clip_event_name_is_registered() -> None:
    for name in CLIP_EVENTS:
        assert name in PAYLOAD_MODELS, f"{name} has no payload model registered"


@pytest.mark.parametrize("name", list(CLIP_EVENTS))
def test_round_trip(name: str) -> None:
    payload = CLIP_EVENTS[name]
    validated = validate_payload(name, payload)
    for key, value in payload.items():
        assert validated[key] == value


def test_clip_requested_defaults() -> None:
    validated = validate_payload(E.CLIP_REQUESTED, {"trigger_kind": "chat_hype", "source": "event"})
    assert validated["origin_event_id"] == ""
    assert validated["session_id"] == ""
    assert validated["tags"] == []

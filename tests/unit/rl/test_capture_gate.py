"""#27: the shared RL screen-capture privacy gate. Stage 1's `_recognize_loop` and Stage 2's
`_vision_loop` (`plugins/rl/src/nox_plugin_rl/plugin.py`) are meant to both drive one of these from
`privacy.capture_changed` (mirroring `nox.rl.vision`'s "core owns the DB/policy-shaped logic, the
plugin only reacts to events" split) instead of each hand-rolling their own boolean flag."""

from __future__ import annotations

from typing import Any

from nox.rl.capture_gate import CaptureGate


class FakeLog:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **kwargs: Any) -> None:
        self.calls.append((event, kwargs))


class FakeCapture:
    """Stand-in for the real `capture_frame` - counts calls and returns a sentinel."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        return "frame"


def test_starts_allowed_matching_stage_2s_existing_default() -> None:
    gate = CaptureGate()
    assert gate.allowed is True
    assert gate.reason == ""


async def test_gate_closed_capture_function_never_called() -> None:
    gate = CaptureGate(log=FakeLog())
    gate.on_capture_changed({"screen": False})
    capture = FakeCapture()

    result = await gate.maybe_capture(capture)

    assert result is None
    assert capture.calls == 0  # no frame grabbed, not merely a discarded one


async def test_reopen_resumes_capture() -> None:
    gate = CaptureGate(log=FakeLog())
    gate.on_capture_changed({"screen": False})
    gate.on_capture_changed({"screen": True})
    capture = FakeCapture()

    result = await gate.maybe_capture(capture)

    assert result == "frame"
    assert capture.calls == 1


async def test_open_gate_still_captures() -> None:
    gate = CaptureGate()
    capture = FakeCapture()

    result = await gate.maybe_capture(capture)

    assert result == "frame"
    assert capture.calls == 1


def test_transitions_logged_once_pause_then_resume() -> None:
    log = FakeLog()
    gate = CaptureGate(log=log)

    gate.on_capture_changed({"screen": False})
    gate.on_capture_changed({"screen": False})  # repeat: no new transition
    gate.on_capture_changed({"screen": False})  # repeat again

    assert [event for event, _ in log.calls] == ["rl.capture_paused"]
    assert log.calls[0][1]["reason"]

    gate.on_capture_changed({"screen": True})
    gate.on_capture_changed({"screen": True})  # repeat: no new transition

    assert [event for event, _ in log.calls] == ["rl.capture_paused", "rl.capture_resumed"]


def test_pause_reason_is_logged() -> None:
    log = FakeLog()
    gate = CaptureGate(log=log)
    gate.on_capture_changed({"screen": False})

    assert gate.reason
    assert log.calls == [("rl.capture_paused", {"reason": gate.reason})]


def test_pause_reason_names_the_active_zone_when_known() -> None:
    log = FakeLog()
    gate = CaptureGate(log=log)
    gate.on_zone_changed({"active": True, "zone": "banking"})

    gate.on_capture_changed({"screen": False})

    assert gate.reason == "zone:banking"
    assert log.calls == [("rl.capture_paused", {"reason": "zone:banking"})]


def test_pause_reason_falls_back_when_no_zone_info_was_forwarded() -> None:
    gate = CaptureGate()
    gate.on_capture_changed({"screen": False})

    assert gate.reason == "privacy.capture.screen_off"


def test_zone_leaving_does_not_by_itself_reopen_the_gate() -> None:
    """`on_zone_changed` only sharpens the pause reason - `on_capture_changed` (driven by
    `PrivacyService.effective_capture()`, which already accounts for the zone) is the sole gate
    signal, so a stray/duplicate `zone_changed` can never accidentally resume capture on its own."""
    gate = CaptureGate()
    gate.on_capture_changed({"screen": False})

    gate.on_zone_changed({"active": False, "zone": None})

    assert gate.allowed is False


async def test_no_publish_while_paused_is_the_callers_responsibility_via_none() -> None:
    """`maybe_capture` returning `None` is the mechanism a loop uses to guarantee it publishes
    nothing derived from a paused period - a loop that skips its publish step on `None` (as
    `_recognize_loop`/`_vision_loop` are meant to) automatically satisfies that requirement."""
    gate = CaptureGate()
    gate.on_capture_changed({"screen": False})
    published: list[str] = []

    async def capture_and_publish() -> str:
        frame = "frame"
        published.append(frame)  # would only run if maybe_capture actually called capture_fn
        return frame

    result = await gate.maybe_capture(capture_and_publish)

    assert result is None
    assert published == []


def test_defaults_paused_when_constructed_closed() -> None:
    gate = CaptureGate(initially_allowed=False)
    assert gate.allowed is False
    assert gate.reason == "privacy.capture.screen_off"


def test_missing_screen_key_defaults_to_allowed() -> None:
    """`payload.get("screen", True)`: a payload missing `screen` entirely (shouldn't happen for a
    real `CaptureChanged`, but keeps this gate defensive like `nox.rl.vision`'s event handlers)
    never surprises a caller by silently pausing."""
    gate = CaptureGate(initially_allowed=False)
    gate.on_capture_changed({})
    assert gate.allowed is True

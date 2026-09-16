"""OP-10: `SpeechPolicy.may_speak` is the single gate for unsolicited speech (greeting/proactive);
`reply` is only ever blocked by mute. Covers every deny reason plus the allow path."""

from __future__ import annotations

from datetime import datetime

import pytest

from nox.core.config import NoxConfig
from nox.core.speech_policy import SpeechPolicy, in_quiet_hours
from tests.unit.fakes import FakeState


def make_policy(
    *,
    muted: bool = False,
    active_zone: str | None = None,
    privacy_mode: str = "balanced",
    config: NoxConfig | None = None,
    clock_hour: int = 12,
) -> SpeechPolicy:
    state = FakeState()
    state.state.assistant.muted = muted
    clock_dt = datetime(2026, 9, 13, clock_hour, 0)
    return SpeechPolicy(
        state=state,
        config=config or NoxConfig(),
        active_zone=lambda: active_zone,
        privacy_mode=lambda: privacy_mode,
        clock=lambda: clock_dt,
    )


@pytest.mark.parametrize("kind", ["greeting", "proactive", "reply"])
def test_allow_path_when_nothing_blocks(kind: str) -> None:
    policy = make_policy()
    assert policy.may_speak(kind) == (True, "ok")  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", ["greeting", "proactive", "reply"])
def test_muted_blocks_every_kind(kind: str) -> None:
    policy = make_policy(muted=True)
    assert policy.may_speak(kind) == (False, "muted")  # type: ignore[arg-type]


def test_reply_ignores_zone_privacy_mode_and_quiet_hours() -> None:
    cfg = NoxConfig()
    policy = make_policy(active_zone="bedroom", privacy_mode="private", config=cfg, clock_hour=2)
    assert policy.may_speak("reply") == (True, "ok")


def test_privacy_zone_blocks_greeting_and_proactive() -> None:
    policy = make_policy(active_zone="bedroom")
    assert policy.may_speak("greeting") == (False, "privacy_zone")
    assert policy.may_speak("proactive") == (False, "privacy_zone")


@pytest.mark.parametrize("mode", ["private", "offline"])
def test_privacy_mode_blocks_when_quiet_in_private_modes_set(mode: str) -> None:
    cfg = NoxConfig()
    assert cfg.pet.quiet_in_private_modes is True  # decided default (OP-10 B)
    policy = make_policy(privacy_mode=mode, config=cfg)
    assert policy.may_speak("greeting") == (False, "privacy_mode")
    assert policy.may_speak("proactive") == (False, "privacy_mode")


@pytest.mark.parametrize("mode", ["private", "offline"])
def test_privacy_mode_allows_when_flag_disabled(mode: str) -> None:
    cfg = NoxConfig.model_validate(
        {
            **NoxConfig().model_dump(mode="json"),
            "pet": {**NoxConfig().pet.model_dump(), "quiet_in_private_modes": False},
        }
    )
    policy = make_policy(privacy_mode=mode, config=cfg)
    assert policy.may_speak("greeting") == (True, "ok")


def test_privacy_mode_full_and_balanced_never_blocked() -> None:
    for mode in ("full", "balanced"):
        policy = make_policy(privacy_mode=mode)
        assert policy.may_speak("greeting") == (True, "ok")


def test_greeting_disabled_only_blocks_greeting() -> None:
    cfg = NoxConfig.model_validate(
        {
            **NoxConfig().model_dump(mode="json"),
            "pet": {**NoxConfig().pet.model_dump(), "greeting_enabled": False},
        }
    )
    policy = make_policy(config=cfg)
    assert policy.may_speak("greeting") == (False, "greeting_disabled")
    assert policy.may_speak("proactive") == (True, "ok")
    assert policy.may_speak("reply") == (True, "ok")


def test_quiet_hours_block_greeting_and_proactive() -> None:
    policy = make_policy(clock_hour=23)
    assert policy.may_speak("greeting") == (False, "quiet_hours")
    assert policy.may_speak("proactive") == (False, "quiet_hours")
    assert policy.may_speak("reply") == (True, "ok")


def test_quiet_hours_outside_window_allows() -> None:
    policy = make_policy(clock_hour=12)
    assert policy.may_speak("greeting") == (True, "ok")


# ---- ST-19-03: "urgent" kind (bypasses zone/privacy-mode/quiet-hours, never mute) --------------


def test_urgent_bypasses_zone_privacy_mode_and_quiet_hours() -> None:
    cfg = NoxConfig()
    policy = make_policy(active_zone="bedroom", privacy_mode="offline", config=cfg, clock_hour=2)
    assert policy.may_speak("urgent") == (True, "ok")


def test_urgent_is_blocked_only_by_mute() -> None:
    policy = make_policy(muted=True)
    assert policy.may_speak("urgent") == (False, "muted")


@pytest.mark.parametrize("kind", ["greeting", "proactive", "reply", "urgent"])
def test_all_four_kinds_covered_by_allow_path(kind: str) -> None:
    policy = make_policy()
    assert policy.may_speak(kind) == (True, "ok")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("start", "end", "hour", "expected"),
    [
        ("23:00", "08:00", 23, True),  # inside the overnight wrap, at start
        ("23:00", "08:00", 3, True),  # inside the overnight wrap, past midnight
        ("23:00", "08:00", 8, False),  # end is exclusive
        ("23:00", "08:00", 12, False),  # clearly daytime
        ("09:00", "17:00", 12, True),  # same-day window
        ("09:00", "17:00", 20, False),  # outside a same-day window
        ("09:00", "09:00", 9, False),  # zero-length window never counts as quiet
    ],
)
def test_in_quiet_hours_table(start: str, end: str, hour: int, expected: bool) -> None:
    from datetime import time

    from nox.core.config import QuietHoursConfig

    quiet = QuietHoursConfig(start=start, end=end)
    assert in_quiet_hours(quiet, time(hour, 0)) is expected

"""nox.memory.importance: explicit-command top priority, passive throttling (ST-07-01 AC1/AC2)."""

from __future__ import annotations

from nox.memory.importance import (
    EXPLICIT_IMPORTANCE,
    PassiveThrottle,
    is_explicit_command,
    score_importance,
)


def test_explicit_command_outranks_passive_heuristics() -> None:
    explicit = score_importance("Merk dir bitte: mein Lieblingstee ist Earl Grey.")
    passive = score_importance("Ich hatte heute einen langen Tag im Büro und bin müde.")
    assert explicit == EXPLICIT_IMPORTANCE
    assert passive < explicit


def test_is_explicit_command_detects_markers() -> None:
    assert is_explicit_command("Denk dran, ich mag keinen Kaffee.")
    assert is_explicit_command("Please remember that I prefer dark mode.")
    assert not is_explicit_command("What's the weather like today?")


def test_explicit_flag_overrides_autodetect() -> None:
    # Caller already knows (e.g. a dedicated `memory.write` tool call) - forcing explicit=True
    # scores top priority even without a marker phrase, and False caps it even with one.
    assert score_importance("just some fact", explicit=True) == EXPLICIT_IMPORTANCE
    assert score_importance("merk dir das", explicit=False) < EXPLICIT_IMPORTANCE


def test_passive_score_bounded_below_explicit() -> None:
    long_text = "x" * 200
    score = score_importance(long_text, explicit=False)
    assert 0.0 < score < EXPLICIT_IMPORTANCE


def test_passive_throttle_rate_limits() -> None:
    t = [0.0]
    throttle = PassiveThrottle(
        min_interval_s=10.0, max_per_window=5, window_s=60.0, clock=lambda: t[0]
    )
    assert throttle.allow() is True
    assert throttle.allow() is False  # too soon (min_interval_s)
    t[0] = 15.0
    assert throttle.allow() is True


def test_passive_throttle_window_cap() -> None:
    t = [0.0]
    throttle = PassiveThrottle(
        min_interval_s=0.0, max_per_window=2, window_s=100.0, clock=lambda: t[0]
    )
    assert throttle.allow() is True
    t[0] = 1.0
    assert throttle.allow() is True
    t[0] = 2.0
    assert throttle.allow() is False  # window cap reached

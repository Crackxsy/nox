"""`RateLimiter` - the 20 msg/30s cap and the >=1.5s minimum gap, independently and configurable."""

from __future__ import annotations

from nox_plugin_twitch.ratelimit import RateLimiter


def test_first_message_is_always_allowed() -> None:
    limiter = RateLimiter(max_messages=20, window_s=30.0, min_gap_s=1.5)
    ok, reason = limiter.try_acquire()
    assert ok is True
    assert reason == ""


def test_minimum_gap_is_enforced() -> None:
    now = [0.0]
    limiter = RateLimiter(max_messages=20, window_s=30.0, min_gap_s=1.5, clock=lambda: now[0])
    ok1, _ = limiter.try_acquire()
    now[0] = 0.5  # under the 1.5s gap
    ok2, reason2 = limiter.try_acquire()
    assert ok1 is True
    assert ok2 is False
    assert "gap" in reason2


def test_minimum_gap_resets_after_enough_time() -> None:
    now = [0.0]
    limiter = RateLimiter(max_messages=20, window_s=30.0, min_gap_s=1.5, clock=lambda: now[0])
    limiter.try_acquire()
    now[0] = 2.0
    ok, _ = limiter.try_acquire()
    assert ok is True


def test_max_messages_per_window_is_enforced() -> None:
    now = [0.0]
    limiter = RateLimiter(max_messages=3, window_s=30.0, min_gap_s=0.0, clock=lambda: now[0])
    for i in range(3):
        now[0] = float(i)
        ok, _ = limiter.try_acquire()
        assert ok is True
    now[0] = 3.0
    ok, reason = limiter.try_acquire()
    assert ok is False
    assert "rate limit" in reason


def test_window_expiry_allows_more_messages() -> None:
    now = [0.0]
    limiter = RateLimiter(max_messages=2, window_s=10.0, min_gap_s=0.0, clock=lambda: now[0])
    limiter.try_acquire()
    now[0] = 1.0
    limiter.try_acquire()
    now[0] = 2.0
    ok_within_window, _ = limiter.try_acquire()
    assert ok_within_window is False
    now[0] = 20.0  # past the 10s window for the first two sends
    ok_after_expiry, _ = limiter.try_acquire()
    assert ok_after_expiry is True


def test_check_does_not_record() -> None:
    limiter = RateLimiter(max_messages=1, window_s=30.0, min_gap_s=0.0)
    ok1, _ = limiter.check()
    ok2, _ = limiter.check()
    assert ok1 is True
    assert ok2 is True  # check() alone never consumes the budget


def test_twenty_first_message_is_rejected_with_default_burst_limit() -> None:
    """Same shape as `tests/integration/test_twitch_plugin.py`'s rate-limit assertion: a burst of
    21 sends with the gap constraint out of the way (`min_gap_s=0`) hits the 20/30s count cap."""
    now = [0.0]
    limiter = RateLimiter(max_messages=20, window_s=30.0, min_gap_s=0.0, clock=lambda: now[0])
    for _ in range(20):
        ok, _ = limiter.try_acquire()
        assert ok is True
    ok, reason = limiter.try_acquire()
    assert ok is False
    assert "rate limit" in reason

"""Attention matrix support: effective proactivity ceiling per mode (A.16 "modes change density"),
an hourly interruption budget (ST-19-05), and focus-mode detection - the pieces `notify()` needs
before it ever asks `SpeechPolicy` anything. Quiet hours are `nox.core.speech_policy.in_quiet_hours`
reused as-is (not re-implemented here).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import datetime

from nox.core.config import AttentionConfig


def effective_ceiling(config: AttentionConfig, mode: str) -> int:
    """The 0..5 proactivity ceiling in force right now: the mode-specific value if one is
    configured (A.16: "behaviour, density, tone and proactivity change per mode"), clamped to
    never exceed the global ceiling; falls back to the global ceiling when the mode is unlisted."""
    global_ceiling = config.proactivity_level
    per_mode = config.per_mode.get(mode)
    if per_mode is None:
        return global_ceiling
    return min(per_mode, global_ceiling)


def is_focus_mode(config: AttentionConfig, mode: str) -> bool:
    """A ceiling of 0 - whether because `mode == "focus"` or an operator zeroed a mode's
    ceiling directly - means no proactive hints at all; ST-19-05's "focus mode" is exactly that
    state, not a separate flag to keep in sync with the mode ceiling table."""
    return effective_ceiling(config, mode) <= 0


class InterruptionBudget:
    """Rolling one-hour counter of proactive interruptions actually delivered (ST-19-05).
    URGENT never consumes budget - only `kind="proactive"` hints that were actually spoken do."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now())
        self._events: deque[datetime] = deque()

    def _prune(self, now: datetime) -> None:
        while self._events and (now - self._events[0]).total_seconds() > 3600:
            self._events.popleft()

    def used(self) -> int:
        self._prune(self._clock())
        return len(self._events)

    def allow(self, ceiling: int) -> bool:
        """Whether one more interruption fits under `ceiling` (per hour) right now."""
        if ceiling <= 0:
            return False
        return self.used() < ceiling

    def record(self) -> None:
        self._events.append(self._clock())

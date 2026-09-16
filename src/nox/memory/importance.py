"""Multi-stage importance scoring for memory candidates (ST-07-01, FR-7.5/FR-7.6).

An explicit user command ("merk dir das") always outranks passive-chat heuristics; passive
"should I remember?" checks are throttled by `PassiveThrottle` rather than firing on every turn.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

EXPLICIT_IMPORTANCE = 1.0
_EXPLICIT_MARKERS = (
    "merk dir",
    "merke dir",
    "denk dran",
    "vergiss nicht",
    "remember this",
    "remember that",
    "keep in mind",
    "note that down",
)


# FR-7.5: fixed type list. Deliberately no "Health" type (health data lives in health_history, not
# memory_items).
class MemoryType(StrEnum):
    USER = "user"
    PROJECT = "project"
    CONVERSATION = "conversation"
    TASK = "task"
    DECISION = "decision"
    SKILL = "skill"
    COACHING = "coaching"
    STREAM = "stream"
    RELATIONSHIP = "relationship"
    PREFERENCE = "preference"
    MOOD_STATE = "mood_state"
    NOX_SELF = "nox_self"
    EVENT = "event"
    ACHIEVEMENT = "achievement"


_PASSIVE_BASE = 0.35


def is_explicit_command(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _EXPLICIT_MARKERS)


def score_importance(
    text: str,
    *,
    explicit: bool | None = None,
    length_bonus: bool = True,
) -> float:
    """Score in [0, 1]. `explicit=None` auto-detects from `text`; pass `True`/`False` when the
    caller already knows (e.g. a dedicated tool call vs. passive transcript scanning)."""
    if explicit is None:
        explicit = is_explicit_command(text)
    if explicit:
        return EXPLICIT_IMPORTANCE
    score = _PASSIVE_BASE
    if length_bonus and len(text.strip()) > 120:
        score += 0.1
    return min(score, 0.9)  # passive candidates never reach explicit-command importance


@dataclass
class PassiveThrottle:
    """Rate-limits "should I remember?" passive checks (FR-7.6: "heavily throttled").

    `min_interval_s` between checks and `max_per_window`/`window_s` as a secondary cap.
    """

    min_interval_s: float = 30.0
    max_per_window: int = 3
    window_s: float = 300.0
    clock: Callable[[], float] = field(default=time.monotonic)
    _last: float = field(default=-1.0, init=False, repr=False)
    _events: list[float] = field(default_factory=list, init=False, repr=False)

    def allow(self) -> bool:
        now = self.clock()
        if self._last >= 0 and now - self._last < self.min_interval_s:
            return False
        self._events = [t for t in self._events if now - t <= self.window_s]
        if len(self._events) >= self.max_per_window:
            return False
        self._last = now
        self._events.append(now)
        return True

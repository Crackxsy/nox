""": a bounded, traceable feedback loop. Learning may only turn hint *frequency* down (or back up)
per
event-type/context key - it can never be applied to URGENT security/data-loss cases, which is
enforced by callers simply never looking a weight up for those (see `service.notify`).

"Bounded": every weight is clamped to `[config.feedback_min_weight, config.feedback_max_weight]`.
"Traceable": every adjustment is appended to a capped in-memory log with the key, delta and
resulting weight, so a `proactive.status.read` caller (or a test) can see exactly why a weight is
what it is - never a black-box gradient update.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WeightAdjustment:
    key: str
    accepted: bool
    delta: float
    new_weight: float


class FeedbackWeights:
    def __init__(
        self, *, min_weight: float = 0.2, max_weight: float = 1.0, step: float = 0.05
    ) -> None:
        if min_weight > max_weight:
            raise ValueError("min_weight must be <= max_weight")
        self._min = min_weight
        self._max = max_weight
        self._step = step
        self._weights: dict[str, float] = {}
        self._trace: list[WeightAdjustment] = []

    def weight(self, key: str) -> float:
        """Current multiplier for `key` (defaults to `max_weight`: no reason yet to hold back)."""
        return self._weights.get(key, self._max)

    def adjust(self, key: str, *, accepted: bool) -> float:
        """Nudge `key`'s weight up on acceptance, down on dismissal; always clamped, always traced.
        Returns the new weight."""
        current = self.weight(key)
        delta = self._step if accepted else -self._step
        new_weight = min(self._max, max(self._min, current + delta))
        self._weights[key] = new_weight
        self._trace.append(
            WeightAdjustment(key=key, accepted=accepted, delta=delta, new_weight=new_weight)
        )
        if len(self._trace) > 500:  # bounded: a feedback log is a debugging aid, not an archive
            self._trace.pop(0)
        return new_weight

    def trace(self, limit: int = 50) -> list[WeightAdjustment]:
        return self._trace[-limit:]

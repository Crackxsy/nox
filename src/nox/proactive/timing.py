"""ST-19-06: a small timing learner biasing *when* non-urgent hints are offered toward hours that
have historically been well received. Data-sparing by construction (NFR-3/FR-14.4 spirit): the only
signal ever recorded is `(hour_of_day, accepted)` - never message content, never a timestamp beyond
the hour bucket - aggregated into one running acceptance rate per of the 24 hours-of-day.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

_HOURS = 24
_DEFAULT_SCORE = 0.5  # neutral prior: no bias until we have observations


class TimingLearner(BaseModel):
    """`scores[h]` is an EWMA (alpha=`rate`) of accept/dismiss outcomes seen at hour `h` (0..23)."""

    rate: float = Field(default=0.2, gt=0.0, le=1.0)
    scores: list[float] = Field(default_factory=lambda: [_DEFAULT_SCORE] * _HOURS)
    counts: list[int] = Field(default_factory=lambda: [0] * _HOURS)

    def record(self, hour: int, accepted: bool) -> None:
        if not 0 <= hour < _HOURS:
            raise ValueError(f"hour must be 0..23, got {hour}")
        outcome = 1.0 if accepted else 0.0
        self.scores[hour] = (1 - self.rate) * self.scores[hour] + self.rate * outcome
        self.counts[hour] += 1

    def score(self, hour: int) -> float:
        """0..1 preference for offering a hint at this hour; `_DEFAULT_SCORE` until observed."""
        if not 0 <= hour < _HOURS:
            raise ValueError(f"hour must be 0..23, got {hour}")
        return self.scores[hour]

    def prefers_now(self, hour: int, *, threshold: float = 0.35) -> bool:
        """False only once there is enough signal (>=3 observations) that this hour is poorly
        received; unobserved or borderline hours default to allowing the hint through."""
        if self.counts[hour] < 3:
            return True
        return self.score(hour) >= threshold

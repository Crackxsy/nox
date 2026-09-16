"""ST-19-06: the timing learner is data-sparing (hour-of-day + accepted only) and bounded."""

from __future__ import annotations

import pytest

from nox.proactive.timing import TimingLearner


def test_unobserved_hour_defaults_to_neutral_and_allowed() -> None:
    learner = TimingLearner()
    assert learner.score(9) == pytest.approx(0.5)
    assert learner.prefers_now(9) is True


def test_repeated_dismissals_lower_the_score() -> None:
    learner = TimingLearner()
    for _ in range(5):
        learner.record(9, accepted=False)
    assert learner.score(9) < 0.3


def test_repeated_dismissals_eventually_block_that_hour() -> None:
    learner = TimingLearner()
    for _ in range(5):
        learner.record(14, accepted=False)
    assert learner.prefers_now(14) is False


def test_acceptance_recovers_the_score() -> None:
    learner = TimingLearner()
    for _ in range(5):
        learner.record(10, accepted=False)
    for _ in range(5):
        learner.record(10, accepted=True)
    assert learner.score(10) > 0.5


def test_scores_stay_within_bounds() -> None:
    learner = TimingLearner()
    for _ in range(50):
        learner.record(0, accepted=True)
    assert 0.0 <= learner.score(0) <= 1.0
    for _ in range(50):
        learner.record(0, accepted=False)
    assert 0.0 <= learner.score(0) <= 1.0


def test_rejects_out_of_range_hour() -> None:
    learner = TimingLearner()
    with pytest.raises(ValueError):
        learner.record(24, accepted=True)
    with pytest.raises(ValueError):
        learner.score(-1)

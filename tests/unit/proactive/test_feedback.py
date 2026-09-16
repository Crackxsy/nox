"""ST-19-07: the feedback loop is bounded (clamped to [min_weight, max_weight]) and traceable
(every adjustment is logged with its delta and resulting weight)."""

from __future__ import annotations

from nox.proactive.feedback import FeedbackWeights


def test_unknown_key_defaults_to_max_weight() -> None:
    weights = FeedbackWeights(min_weight=0.2, max_weight=1.0)
    assert weights.weight("rl.callout") == 1.0


def test_dismissals_lower_the_weight_down_to_the_floor_never_below() -> None:
    weights = FeedbackWeights(min_weight=0.2, max_weight=1.0, step=0.3)
    for _ in range(20):
        weights.adjust("rl.callout", accepted=False)
    assert weights.weight("rl.callout") == 0.2


def test_acceptances_raise_the_weight_up_to_the_ceiling_never_above() -> None:
    weights = FeedbackWeights(min_weight=0.2, max_weight=1.0, step=0.3)
    weights.adjust("rl.callout", accepted=False)  # 0.7
    for _ in range(20):
        weights.adjust("rl.callout", accepted=True)
    assert weights.weight("rl.callout") == 1.0


def test_trace_records_every_adjustment_and_is_bounded() -> None:
    weights = FeedbackWeights()
    for i in range(600):
        weights.adjust("k", accepted=i % 2 == 0)
    trace = weights.trace(limit=1000)
    assert len(trace) <= 500  # the internal log itself is capped
    last = trace[-1]
    assert last.key == "k"
    assert last.new_weight == weights.weight("k")


def test_keys_are_independent() -> None:
    weights = FeedbackWeights(max_weight=1.0)
    weights.adjust("a", accepted=False)
    assert weights.weight("a") < 1.0
    assert weights.weight("b") == 1.0  # untouched key stays at the default ceiling

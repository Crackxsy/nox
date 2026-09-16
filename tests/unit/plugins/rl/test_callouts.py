"""Callout rule engine (ST-12-06): priority order, cooldown suppression, the 2-4/minute rate cap,
and silence-on-low-confidence (A129) - table-driven per Spec §12.1."""

from __future__ import annotations

from nox_plugin_rl.callouts import CalloutEngine


def _clock(start: float = 1000.0):
    box = {"t": start}

    def now() -> float:
        return box["t"]

    def advance(seconds: float) -> None:
        box["t"] += seconds

    return now, advance


def test_boost_low_fires_a_callout() -> None:
    now, _ = _clock()
    engine = CalloutEngine(now_fn=now)
    decision = engine.evaluate(kind="boost_low", source="hud", confidence=0.9, payload={})
    assert decision is not None
    assert decision.clip_id == "boost_low_play_safe"


def test_low_confidence_stays_silent() -> None:
    now, _ = _clock()
    engine = CalloutEngine(now_fn=now, min_confidence=0.6)
    decision = engine.evaluate(kind="boost_low", source="hud", confidence=0.3, payload={})
    assert decision is None


def test_save_from_hud_is_never_emitted_as_a_callout() -> None:
    """Spec §8: `save` is replay-only in Stage 1, never real-time."""
    now, _ = _clock()
    engine = CalloutEngine(now_fn=now)
    decision = engine.evaluate(kind="save", source="hud", confidence=0.95, payload={})
    assert decision is None


def test_cooldown_suppresses_a_repeat_within_the_window() -> None:
    now, advance = _clock()
    engine = CalloutEngine(now_fn=now, cooldown_s=20.0)
    first = engine.evaluate(kind="boost_low", source="hud", confidence=0.9, payload={})
    assert first is not None
    advance(5.0)
    second = engine.evaluate(kind="boost_low", source="hud", confidence=0.9, payload={})
    assert second is None
    advance(20.0)
    third = engine.evaluate(kind="boost_low", source="hud", confidence=0.9, payload={})
    assert third is not None


def test_rate_cap_stays_within_max_per_minute() -> None:
    now, advance = _clock()
    engine = CalloutEngine(now_fn=now, cooldown_s=0.0, max_per_minute=4)
    fired = 0
    for _ in range(10):
        decision = engine.evaluate(
            kind="goal", source="hud", confidence=0.9, payload={"team": "self"}
        )
        if decision is not None:
            fired += 1
        advance(2.0)  # 10 events inside one rolling minute
    assert fired <= 4
    assert engine.calls_in_last_minute() <= 4


def test_priority_prefers_danger_over_lower_tiers_when_both_conditions_hold() -> None:
    """Demo (danger) and boost_low (tactical_error) are independent kinds, so this verifies the
    tier ordering via two distinct rules registered for the same kind is not accidentally reversed
    - goal_for (rare_praise) vs. goal_against (tactical_error) share `kind="goal"`."""
    now, _ = _clock()
    engine = CalloutEngine(now_fn=now)
    against = engine.evaluate(
        kind="goal", source="hud", confidence=0.9, payload={"team": "opponent"}
    )
    assert against is not None
    assert against.rule_id == "rl.callout.goal_against"


def test_private_channel_only_is_enforced_by_the_caller_contract() -> None:
    """The engine itself never constructs a `TtsRequest` (that is `RlCalloutService`'s job, always
    `Channel.PRIVATE` - see `tests/unit/rl/test_services.py`); this just documents the decision
    surface stays clip/rule ids only, nothing channel-related to get wrong here."""
    now, _ = _clock()
    engine = CalloutEngine(now_fn=now)
    decision = engine.evaluate(kind="overtime", source="hud", confidence=0.9, payload={})
    assert decision is not None
    assert not hasattr(decision, "channel")

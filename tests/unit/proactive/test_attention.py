"""ST-19-05: effective per-mode ceiling, focus-mode detection, and the hourly interruption
budget."""

from __future__ import annotations

from datetime import datetime, timedelta

from nox.core.config import AttentionConfig
from nox.proactive.attention import InterruptionBudget, effective_ceiling, is_focus_mode


def test_effective_ceiling_uses_global_when_mode_unlisted() -> None:
    cfg = AttentionConfig(proactivity_level=3, per_mode={})
    assert effective_ceiling(cfg, "coding") == 3


def test_effective_ceiling_uses_per_mode_value() -> None:
    cfg = AttentionConfig(proactivity_level=3, per_mode={"stream": 4, "focus": 0})
    assert effective_ceiling(cfg, "stream") == 3  # clamped to the global ceiling
    assert effective_ceiling(cfg, "focus") == 0


def test_effective_ceiling_clamps_to_global_ceiling() -> None:
    cfg = AttentionConfig(proactivity_level=1, per_mode={"stream": 4})
    assert effective_ceiling(cfg, "stream") == 1


def test_is_focus_mode_true_only_at_zero_ceiling() -> None:
    cfg = AttentionConfig(proactivity_level=3, per_mode={"focus": 0, "coding": 2})
    assert is_focus_mode(cfg, "focus") is True
    assert is_focus_mode(cfg, "coding") is False


def test_interruption_budget_allows_up_to_ceiling_then_blocks() -> None:
    now = datetime(2026, 9, 14, 12, 0)
    budget = InterruptionBudget(clock=lambda: now)
    assert budget.allow(2) is True
    budget.record()
    assert budget.allow(2) is True
    budget.record()
    assert budget.allow(2) is False
    assert budget.used() == 2


def test_interruption_budget_zero_ceiling_never_allows() -> None:
    budget = InterruptionBudget(clock=lambda: datetime(2026, 9, 14, 12, 0))
    assert budget.allow(0) is False


def test_interruption_budget_prunes_events_older_than_one_hour() -> None:
    t = [datetime(2026, 9, 14, 12, 0)]
    budget = InterruptionBudget(clock=lambda: t[0])
    budget.record()
    budget.record()
    assert budget.used() == 2
    t[0] = t[0] + timedelta(hours=1, seconds=1)
    assert budget.used() == 0
    assert budget.allow(1) is True

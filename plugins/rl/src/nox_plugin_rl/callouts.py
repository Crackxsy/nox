"""Prepared-clip callout engine: priority (danger > tactical_error > pattern > opportunity >
rare_praise, F131), cooldown + 2-4/minute rate cap (A131), silence-on-low-confidence (A129). Only
wires rules whose trigger is satisfiable from the `rl.event` kinds Stage 1 actually produces (goal,
overtime, boost_low, demo - `save` is replay-only, never real-time; AC).

The initial A130 clip list ("Don't challenge, you're last.", "Rotate back post.",...) is mostly
position-based and therefore *not* reachable in Stage 1 (no object detection until v0.9's Stage 2,
Spec) - this engine registers only the HUD-derivable subset plus reasonable additions for
goal/overtime, and leaves the rest as an explicit, documented gap rather than registering
unreachable rules."""

from __future__ import annotations

import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_TIER_PRIORITY = {
    "danger": 0,
    "tactical_error": 1,
    "pattern": 2,
    "opportunity": 3,
    "rare_praise": 4,
}


@dataclass(frozen=True, slots=True)
class CalloutRule:
    id: str
    tier: str  # danger | tactical_error | pattern | opportunity | rare_praise (F131)
    kind: str  # rl.event kind this reacts to
    clip_id: str
    fallback_text: str
    condition: Callable[[dict[str, Any]], bool] = field(default=lambda payload: True)


@dataclass(frozen=True, slots=True)
class CalloutDecision:
    rule_id: str
    clip_id: str
    fallback_text: str


def _team_scored(payload: dict[str, Any], team: str) -> bool:
    return str(payload.get("team", "")) == team


#: Stage 1's reachable rule set (Spec honest per-kind note; AC). Kickoff-only-with-
#: value (Spec/A131) is not wired: event catalogue has no distinct `kickoff` kind
#: (only a "kickoff/replay camera" banner category, folded into no emitted `rl.event` kind) -
#: flagged as an open point rather than guessed at.
DEFAULT_RULES: tuple[CalloutRule, ...] = (
    CalloutRule(
        id="rl.callout.demo",
        tier="danger",
        kind="demo",
        clip_id="careful_demo",
        fallback_text="Careful, demo.",
    ),
    CalloutRule(
        id="rl.callout.boost_low",
        tier="tactical_error",
        kind="boost_low",
        clip_id="boost_low_play_safe",
        fallback_text="Low boost, play safe.",
    ),
    CalloutRule(
        id="rl.callout.goal_against",
        tier="tactical_error",
        kind="goal",
        clip_id="goal_against_reset",
        fallback_text="Reset, next kickoff.",
        condition=lambda p: _team_scored(p, "opponent"),
    ),
    CalloutRule(
        id="rl.callout.overtime",
        tier="opportunity",
        kind="overtime",
        clip_id="overtime_stay_focused",
        fallback_text="Overtime. Stay focused.",
    ),
    CalloutRule(
        id="rl.callout.goal_for",
        tier="rare_praise",
        kind="goal",
        clip_id="goal_for_nice",
        fallback_text="Nice goal.",
        condition=lambda p: _team_scored(p, "self"),
    ),
)


class CalloutEngine:
    """Stateful: cooldowns and the rolling one-minute rate cap persist across calls (one instance
    per plugin process / live match session)."""

    def __init__(
        self,
        *,
        rules: tuple[CalloutRule, ...] = DEFAULT_RULES,
        min_confidence: float = 0.6,
        cooldown_s: float = 20.0,
        min_per_minute: int = 2,
        max_per_minute: int = 4,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rules = rules
        self._min_confidence = min_confidence
        self._cooldown_s = cooldown_s
        self._min_per_minute = min_per_minute
        self._max_per_minute = max_per_minute
        self._now = now_fn
        self._last_fired: dict[str, float] = {}
        self._recent: deque[float] = deque()

    def _prune(self, now: float) -> None:
        while self._recent and now - self._recent[0] > 60.0:
            self._recent.popleft()

    def evaluate(
        self, *, kind: str, source: str, confidence: float, payload: dict[str, Any] | None = None
    ) -> CalloutDecision | None:
        """One `rl.event` in, at most one `CalloutDecision` out. Never raises."""
        if confidence < self._min_confidence:
            return None  # silence over guessing (A129)
        if kind == "save" and source != "replay":
            return None  # Spec: `save` is never real-time in Stage 1
        payload = payload or {}
        candidates = [r for r in self._rules if r.kind == kind and r.condition(payload)]
        if not candidates:
            return None
        candidates.sort(key=lambda r: _TIER_PRIORITY[r.tier])
        now = self._now()
        self._prune(now)
        for rule in candidates:
            last = self._last_fired.get(rule.clip_id)
            if last is not None and now - last < self._cooldown_s:
                continue
            if len(self._recent) >= self._max_per_minute:
                continue
            self._last_fired[rule.clip_id] = now
            self._recent.append(now)
            return CalloutDecision(
                rule_id=rule.id, clip_id=rule.clip_id, fallback_text=rule.fallback_text
            )
        return None

    def calls_in_last_minute(self) -> int:
        self._prune(self._now())
        return len(self._recent)


def build_utterance_id() -> str:
    return uuid.uuid4().hex

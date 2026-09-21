"""OP-10: the single gate every unsolicited-speech caller (greeting now, proactive speech later,
must go through, so the rule lives in one place instead of being re-implemented per
caller. Implements the decided policy (Decision Plan 2026-09-11 OP-10, matching A480/A482,
speak a greeting or a proactive utterance only when the assistant is not
muted, no privacy zone is active, the privacy mode does not forbid it, `pet.greeting_enabled` (for
greetings) is on, and it is outside quiet hours. A `reply` is a direct answer to something the
user just said or asked for - it is blocked only by mute.

A fourth kind, `"urgent"`, bypasses
zone/privacy-mode/quiet-hours (never mute) for the genuine safety/data-loss class of interruption
- callers in `nox.proactive` decide, per B.13's category ordering, which categories are allowed to
call `may_speak("urgent")` at all (security/system and backup/data-loss only); lesser urgent
categories (resources, task results) must go through `"proactive"` instead so they stay a visible
warning rather than an interruption.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, time
from typing import Literal, Protocol

from nox.core.config import NoxConfig, QuietHoursConfig

SpeechKind = Literal["greeting", "proactive", "reply", "urgent"]

#: Privacy modes that count as "quiet" for OP-10 when `pet.quiet_in_private_modes` is set.
_QUIET_PRIVACY_MODES = frozenset({"private", "offline"})


class StateReader(Protocol):
    """The subset of `NoxStateManager` (and `tests.unit.fakes.FakeState`) this policy needs."""

    def get(self, path: str) -> object: ...


def _parse_hhmm(value: str) -> time:
    hour_str, _, minute_str = value.partition(":")
    return time(int(hour_str), int(minute_str))


def in_quiet_hours(quiet: QuietHoursConfig, now: time) -> bool:
    """Whether `now` falls within `[start, end)`, wrapping past midnight when `start > end`."""
    start, end = _parse_hhmm(quiet.start), _parse_hhmm(quiet.end)
    if start == end:
        return False  # a zero-length window never counts as quiet
    if start < end:
        return start <= now < end
    return now >= start or now < end


class SpeechPolicy:
    """`may_speak(kind)` decides whether the assistant may speak unprompted right now."""

    def __init__(
        self,
        *,
        state: StateReader,
        config: NoxConfig,
        active_zone: Callable[[], str | None],
        privacy_mode: Callable[[], str],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._state = state
        self._config = config
        self._active_zone = active_zone
        self._privacy_mode = privacy_mode
        self._clock = clock or (lambda: datetime.now().astimezone())

    def may_speak(self, kind: SpeechKind) -> tuple[bool, str]:
        """Returns `(allowed, reason)`; `reason` is `"ok"` when allowed, else why it was denied."""
        if bool(self._state.get("assistant.muted")):
            return False, "muted"
        if kind in ("reply", "urgent"):
            return True, "ok"
        if self._active_zone() is not None:
            return False, "privacy_zone"
        pet = self._config.pet
        if pet.quiet_in_private_modes and self._privacy_mode() in _QUIET_PRIVACY_MODES:
            return False, "privacy_mode"
        if kind == "greeting" and not pet.greeting_enabled:
            return False, "greeting_disabled"
        if in_quiet_hours(self._config.attention.quiet_hours, self._clock().time()):
            return False, "quiet_hours"
        return True, "ok"

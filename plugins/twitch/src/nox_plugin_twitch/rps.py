"""`!rps` - deterministic rock/paper/scissors (ST-11-04, Spec v0.2 Stream Bot §3.5/§9). The game
resolution logic is pure and deterministic given both choices; the bot's own choice comes from an
injectable `choice_provider` (defaults to `random.choice`, overridden by tests for determinism).
Per-viewer cooldown lives here too, one round at a time.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import NamedTuple

Choice = str  # "rock" | "paper" | "scissors"

_CHOICES: tuple[Choice, ...] = ("rock", "paper", "scissors")

#: DE/EN input aliases -> canonical choice.
ALIASES: dict[str, Choice] = {
    "stein": "rock",
    "rock": "rock",
    "schere": "scissors",
    "scissors": "scissors",
    "papier": "paper",
    "paper": "paper",
}
#: Aliases whose reply should be in German.
GERMAN_ALIASES: frozenset[str] = frozenset({"stein", "schere", "papier"})

#: winner -> loser (winner beats loser)
_BEATS: dict[Choice, Choice] = {"rock": "scissors", "scissors": "paper", "paper": "rock"}


class RpsResult(NamedTuple):
    bot_choice: Choice
    outcome: str  # "win" | "lose" | "tie" (from the viewer's perspective)


class RockPaperScissors:
    def __init__(
        self,
        *,
        cooldown_s: float = 30.0,
        win_funken: float = 5.0,
        choice_provider: Callable[[], Choice] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.cooldown_s = cooldown_s
        self.win_funken = win_funken
        self._choice_provider = choice_provider or (lambda: random.choice(_CHOICES))  # noqa: S311
        self._clock = clock or time.monotonic
        self._last_played: dict[str, float] = {}

    def cooldown_remaining(self, viewer_id: str) -> float:
        last = self._last_played.get(viewer_id)
        if last is None:
            return 0.0
        return max(0.0, self.cooldown_s - (self._clock() - last))

    def play(self, viewer_id: str, viewer_choice: Choice) -> RpsResult:
        """Resolve one round and record the viewer's cooldown. Caller must validate `viewer_choice`
        is one of `ALIASES.values()` and check `cooldown_remaining` first."""
        self._last_played[viewer_id] = self._clock()
        bot_choice = self._choice_provider()
        if bot_choice == viewer_choice:
            outcome = "tie"
        elif _BEATS[viewer_choice] == bot_choice:
            outcome = "win"
        else:
            outcome = "lose"
        return RpsResult(bot_choice=bot_choice, outcome=outcome)

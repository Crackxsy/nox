"""`TurnBudget`: how much acting one chat turn may do, enforced in code rather than in the prompt.

A model that calls tools in a loop needs a limit, and the limit must hold whatever the model is
told or talked into: so it lives here, in front of the executor, and every native call (Ollama's
`tool_calls`, Claude Code's calls through the MCP bridge, the text-sentinel fallback) is admitted
or refused by the same object.

Three limits, each with its own refusal sentence so the model can tell the user what happened:

* calls per turn (default 20) - "look something up, act, check, report" needs a handful; twenty
  covers sorting a folder file by file without letting a confused model spin forever,
* wall clock per turn (default 180 s) - a chat turn the user waits for; longer work belongs in a
  plan that runs in the background,
* the same failing call three times in a row - retrying a broken call with identical arguments is
  the one loop a model reliably falls into, and it never helps.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "DEFAULT_MAX_CALLS",
    "DEFAULT_MAX_REPEATED_FAILURES",
    "DEFAULT_WALL_CLOCK_S",
    "TurnBudget",
]

DEFAULT_MAX_CALLS = 20
DEFAULT_WALL_CLOCK_S = 180.0
DEFAULT_MAX_REPEATED_FAILURES = 3


def _fingerprint(name: str, arguments: Mapping[str, Any]) -> str:
    return name + json.dumps(arguments, sort_keys=True, default=str, ensure_ascii=False)


@dataclass(slots=True)
class TurnBudget:
    """Budget of one turn. Not shared between turns; not thread-safe (one event loop)."""

    max_calls: int = DEFAULT_MAX_CALLS
    wall_clock_s: float = DEFAULT_WALL_CLOCK_S
    max_repeated_failures: int = DEFAULT_MAX_REPEATED_FAILURES
    clock: Callable[[], float] = time.monotonic
    calls: int = 0
    _started: float = field(init=False)
    _last_failure: str = ""
    _repeated_failures: int = 0

    def __post_init__(self) -> None:
        self._started = self.clock()

    @property
    def elapsed_s(self) -> float:
        return self.clock() - self._started

    @property
    def remaining_s(self) -> float:
        return max(0.0, self.wall_clock_s - self.elapsed_s)

    @property
    def exhausted(self) -> bool:
        """No further call would be admitted, whatever its arguments."""
        return self.calls >= self.max_calls or self.remaining_s <= 0.0

    def admit(self, name: str, arguments: Mapping[str, Any]) -> str | None:
        """`None` if the call may run (and counts it), otherwise the refusal for the model."""
        if self.calls >= self.max_calls:
            return (
                f"the tool budget of this turn is used up ({self.max_calls} calls); "
                "answer with what you have"
            )
        if self.remaining_s <= 0.0:
            return (
                f"this turn has run for {self.wall_clock_s:g} s, the limit for one answer; "
                "answer with what you have and offer to continue"
            )
        if (
            self._repeated_failures >= self.max_repeated_failures
            and _fingerprint(name, arguments) == self._last_failure
        ):
            return (
                f"{name} failed {self._repeated_failures} times in a row with these arguments; "
                "do not retry it, tell the user what went wrong"
            )
        self.calls += 1
        return None

    def record(self, name: str, arguments: Mapping[str, Any], *, ok: bool) -> None:
        """Note the outcome of an admitted call (for the repeated-failure rule)."""
        if ok:
            self._last_failure, self._repeated_failures = "", 0
            return
        fingerprint = _fingerprint(name, arguments)
        if fingerprint == self._last_failure:
            self._repeated_failures += 1
        else:
            self._last_failure, self._repeated_failures = fingerprint, 1

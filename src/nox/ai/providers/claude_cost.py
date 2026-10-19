"""A running, conservative cost estimate of one Claude Code turn, from the streamed usage.

Claude Code reports the real cost only when a turn ends, and `--max-budget-usd` counts the whole
session. A per-turn limit therefore needs an estimate while the turn runs: every model step starts
with `message_start` (its prompt tokens, split into uncached, cache write and cache read) and ends
with `message_delta` (its output tokens so far). Prices are list prices per million tokens of the
current model families; an unknown model is priced like the most expensive family, so the estimate
errs towards stopping early, never late.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["PRICES_PER_MTOK", "TurnCost", "prices_for"]

#: (input, output) USD per million tokens, by model family (list prices, October 2026).
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "haiku": (0.10, 0.50),
    "sonnet": (2.00, 10.00),
    "opus": (4.00, 20.00),
}
#: Cache writes cost more than plain input, cache reads less; the estimate uses the write premium
#: and does not discount reads below a tenth of the input price.
CACHE_WRITE_FACTOR = 1.25
CACHE_READ_FACTOR = 0.10


def prices_for(model: str) -> tuple[float, float]:
    lowered = model.lower()
    for family, prices in PRICES_PER_MTOK.items():
        if family in lowered:
            return prices
    return max(PRICES_PER_MTOK.values())


@dataclass
class TurnCost:
    model: str
    _steps: list[tuple[float, int]] = field(default_factory=list)

    def step_started(
        self, *, input_tokens: int, cache_write_tokens: int, cache_read_tokens: int
    ) -> None:
        price_in, _ = prices_for(self.model)
        prompt = (
            input_tokens
            + cache_write_tokens * CACHE_WRITE_FACTOR
            + cache_read_tokens * CACHE_READ_FACTOR
        )
        self._steps.append((prompt * price_in / 1_000_000, 0))

    def output_so_far(self, output_tokens: int) -> None:
        """Output tokens of the current step (Claude reports a running count)."""
        if not self._steps:
            self.step_started(input_tokens=0, cache_write_tokens=0, cache_read_tokens=0)
        prompt_cost, _ = self._steps[-1]
        self._steps[-1] = (prompt_cost, output_tokens)

    @property
    def usd(self) -> float:
        _, price_out = prices_for(self.model)
        return sum(cost + tokens * price_out / 1_000_000 for cost, tokens in self._steps)

"""A provider's usage limit: what it is, when it ends, and how a provider recognises one.

A subscription (Claude's five-hour and weekly windows) can run out. That is neither an outage nor a
login problem: retrying every minute only produces the same refusal, so the router keeps the
provider out of the chain until the reset. This module owns the value that carries that fact
(`ProviderLimit`) and the two ways to recognise it - the structured `rate_limit_event` of Claude
Code's stream, and, when that is missing, the wording of the error it ends with. It does not decide
what happens next: the router locks the provider, the notices layer tells the user.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict

__all__ = [
    "DEFAULT_LIMIT_RETRY_S",
    "ProviderLimit",
    "describe_limit",
    "limit_for",
    "looks_like_limit",
]

#: How long a provider is kept out when the limit did not say when it resets. Short enough that a
#: limit that ended early is noticed, long enough not to burn a failing request on every turn.
DEFAULT_LIMIT_RETRY_S = 900.0

#: The words Claude Code's final error uses when a plan limit stopped the request and no
#: structured limit event came before it ("5-hour limit reached", "You've hit your limit", "usage
#: limit"). Matched case-insensitively; a login or network error does not contain any of them.
_LIMIT_WORDS = re.compile(
    r"hit your (?:usage |weekly |session )?limit|limit reached|usage limit|rate limit"
    r"|quota (?:exceeded|exhausted|reached)|out of (?:usage|credits)",
    re.IGNORECASE,
)


class ProviderLimit(BaseModel):
    """One provider's exhausted limit. `until` is when the provider may be tried again."""

    model_config = ConfigDict(frozen=True)

    provider: str
    #: Which window ran out: `five_hour`, `seven_day`, `seven_day_opus`, `seven_day_sonnet`,
    #: `overage`, or `unknown` when the provider did not say.
    kind: str = "unknown"
    until: datetime
    #: True when the provider gave no reset time and `until` is only the default retry delay.
    estimated: bool = False

    def active(self, now: datetime) -> bool:
        return now < self.until


def looks_like_limit(message: str) -> bool:
    """Whether an error message is a plan limit rather than any other failure."""
    return _LIMIT_WORDS.search(message) is not None


def describe_limit(limit: ProviderLimit) -> str:
    """One line for a status card: which limit, and when the provider is tried again."""
    until = limit.until.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    when = f"retrying after {until}" if limit.estimated else f"resets {until}"
    return f"usage limit reached ({limit.kind}); {when}"


def limit_for(
    provider: str,
    *,
    kind: str,
    resets_at: float | None,
    now: datetime,
    retry_s: float = DEFAULT_LIMIT_RETRY_S,
) -> ProviderLimit:
    """A `ProviderLimit` from what a provider reported: the window and its reset time (a Unix
    timestamp in seconds), or `None` for "did not say". A reset time that is already past counts as
    not given - a clock that is off must not unlock the provider at once."""
    if resets_at is not None:
        until = datetime.fromtimestamp(resets_at, tz=UTC)
        if until > now:
            return ProviderLimit(provider=provider, kind=kind or "unknown", until=until)
    return ProviderLimit(
        provider=provider,
        kind=kind or "unknown",
        until=now + timedelta(seconds=retry_s),
        estimated=True,
    )

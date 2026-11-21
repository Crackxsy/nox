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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict

__all__ = [
    "DEFAULT_LIMIT_RETRY_S",
    "ProviderLimit",
    "describe_limit",
    "limit_for",
    "looks_like_limit",
    "parse_reset_text",
]

#: How long a provider is kept out when the limit did not say when it resets. Short enough that a
#: limit that ended early is noticed, long enough not to burn a failing request on every turn.
DEFAULT_LIMIT_RETRY_S = 900.0

#: Words that name a plan or quota window: what runs out when a subscription limit is used up.
_WINDOW = (
    r"(?:usage|session|weekly|monthly|daily|hourly|\d+-hour|five-hour|opus|sonnet"
    r"|spend(?:ing)?|plan|message)"
)

#: What the CLI and the API say when a plan limit stopped the request ("You've hit your limit",
#: "5-hour limit reached", "You're out of extra usage", "your org's monthly spend limit",
#: "rate_limit_error"). Matched case-insensitively. A limit of something else - the context
#: window, a connection count, a prompt's length - is a different failure and is not in here.
_LIMIT_WORDS = re.compile(
    r"\bhit your (?:[\w'-]+ ){0,2}limit"
    r"|\b(?:reached|exceeded) (?:your|the) (?:[\w'-]+ ){0,3}" + _WINDOW + r" limit"
    r"|\b" + _WINDOW + r" limits?\b[^.]{0,30}\b(?:reached|exceeded|hit)\b"
    r"|\bout of (?:[\w-]+ )?(?:usage|credits)\b"
    r"|\brate[ _]limit"
    r"|\bquota (?:exceeded|exhausted|reached)\b",
    re.IGNORECASE,
)
_NOT_A_PLAN_LIMIT = re.compile(
    r"\b(?:context|connection|prompt|output|input|file|payload)(?: window| token| size)? limit",
    re.IGNORECASE,
)

_RESET_CLOCK = re.compile(
    r"resets?(?: at)?\s+(?:(?P<month>[A-Z][a-z]{2})[a-z]*\.?\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<half>[ap]m)"
    r"(?:\s*\((?P<zone>[A-Za-z_]+/[A-Za-z_+\-/]+|UTC)\))?",
    re.IGNORECASE,
)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


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
    return _NOT_A_PLAN_LIMIT.search(message) is None and _LIMIT_WORDS.search(message) is not None


def parse_reset_text(message: str, now: datetime) -> float | None:
    """When a limit message says it resets ("resets 11pm (Europe/Berlin)", "resets Mar 12, 3pm"),
    as a Unix timestamp: the next such moment after `now`. `None` when it names no time. A time
    without a zone is read in the machine's zone, one with an unknown zone the same way."""
    found = _RESET_CLOCK.search(message)
    if found is None:
        return None
    twelve, minute = int(found["hour"]), int(found["minute"] or 0)
    if not 1 <= twelve <= 12 or minute > 59:
        return None
    hour = twelve % 12 + (12 if found["half"].lower() == "pm" else 0)
    try:
        zone = ZoneInfo(found["zone"]) if found["zone"] else None
    except (ZoneInfoNotFoundError, ValueError):
        zone = None
    local_now = now.astimezone(zone) if zone else now.astimezone()
    month = found["month"]
    try:
        if month and month[:3].lower() not in _MONTHS:
            return None
        if month:
            target = local_now.replace(
                month=_MONTHS.index(month[:3].lower()) + 1,
                day=int(found["day"]),
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0,
            )
            if target <= local_now:
                target = target.replace(year=target.year + 1)
        else:
            target = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= local_now:
                target += timedelta(days=1)
    except ValueError:
        return None
    return target.timestamp()


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
    message: str = "",
) -> ProviderLimit:
    """A `ProviderLimit` from what a provider reported: the window and its reset time (a Unix
    timestamp in seconds), or `None` for "did not say". A reset time that is already past counts as
    not given - a clock that is off must not unlock the provider at once. Without one, a reset
    time named in `message` is used."""
    if resets_at is None and message:
        resets_at = parse_reset_text(message, now)
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

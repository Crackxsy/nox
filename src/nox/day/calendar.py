"""Calendar feeds: the user's appointments from iCalendar subscriptions.

A feed is a private `.ics` URL - Google Calendar's "secret address in iCal format", a school
timetable's export. The URL *is* the credential, so it lives in the Windows Credential Manager
like a token and is never written to a log, a prompt or an error message: when a fetch fails,
only the exception type and the HTTP status are kept.

Parsing is `icalendar` for the format and `dateutil.rrule` for recurrence (both permissively
licensed; the usual `recurring-ical-events` is LGPL). Occurrences are expanded into a bounded
window around today, which is all a companion needs and keeps a ten-year weekly series cheap.

A feed is untrusted: anyone can drop an invite into a Google calendar, and it then shows up in the
feed unasked. So the expansion is bounded on every axis - recurrences finer than hourly are read
as their first occurrence only, impossible rules are dropped, every series is walked from close to
the window and capped in steps and occurrences, the feed's series share a wall-clock budget, and
at most `MAX_EVENTS_PER_FEED` occurrences are kept. Single appointments are always read, before
any series: a hostile invite can make a feed incomplete, but never hide the user's own day. A
feed that was cut says so (`FeedStatus.truncated`).
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from time import monotonic
from typing import Any

import httpx
import icalendar
from dateutil import tz as dateutil_tz
from dateutil.rrule import rrule, rrulestr
from pydantic import BaseModel, ConfigDict

from nox.core.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "CalendarEvent",
    "CalendarService",
    "CalendarSource",
    "FeedStatus",
    "ParsedFeed",
    "parse_feed",
    "parse_ics",
]


class CalendarEvent(BaseModel):
    """One occurrence, in the local timezone. `end` is exclusive, as in iCalendar."""

    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime
    title: str
    location: str = ""
    all_day: bool = False
    source: str


@dataclass(frozen=True, slots=True)
class CalendarSource:
    """A feed the user can connect. `secret` names the credential that holds its URL."""

    id: str
    label: str
    secret: str


class FeedStatus(BaseModel):
    """What the dashboard and the health check see of one feed."""

    model_config = ConfigDict(frozen=True)

    id: str
    label: str
    configured: bool
    fetched_at: datetime | None = None
    #: Empty when the last fetch worked. Never contains the URL.
    error: str = ""
    count: int = 0
    #: The last fetch had to cut the feed (too many or too complex entries): events may be missing.
    truncated: bool = False


# ---- parsing -------------------------------------------------------------------------------------


#: Longest title or location kept. A feed is untrusted input that ends up in a tool result the
#: model reads, so it is cut to one plain line of bounded length before it goes anywhere.
MAX_TEXT_CHARS = 200

#: A feed larger than this is not a calendar. Google exports of busy calendars are a few hundred
#: kilobytes; the cap keeps a hostile or broken URL from filling memory.
MAX_FEED_BYTES = 5 * 1024 * 1024


#: Recurrence frequencies that are not appointments. A series finer than hourly is read as its
#: first occurrence only: FREQ=SECONDLY over the lookahead window is ~700,000 events.
IGNORED_FREQUENCIES = frozenset({"MINUTELY", "SECONDLY"})

#: Frequencies with a fixed period. Without COUNT, such a series can start its walk at the last
#: period boundary before the window instead of at a DTSTART from decades ago - same occurrences.
#: (A month or a year varies in length; those series are cheap to walk from the start anyway.)
_FIXED_PERIODS: dict[str, timedelta] = {
    "HOURLY": timedelta(hours=1),
    "DAILY": timedelta(days=1),
    "WEEKLY": timedelta(weeks=1),
}

#: `dateutil` stops a rule whose BY* parts never match again only at year 9999 (UNTIL is checked
#: against matches, not against empty periods): thousands of years of empty iterations, seconds
#: per series. The Gregorian calendar repeats exactly every 400 years, weekdays and leap days
#: included, so a series is expanded moved forward by whole cycles to just below 9999, where at
#: most one cycle is left to scan, and moved back afterwards.
_CYCLE_YEARS = 400
_LAST_YEAR = 9998

#: Longest a month can be, per month (leap February), to drop BYMONTH/BYMONTHDAY combinations that
#: can never match (BYMONTH=2;BYMONTHDAY=31) before `dateutil` searches for them.
_MONTH_DAYS = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)

#: Most occurrences one series contributes to the window. An hourly series is 24 a day; anything
#: denser (BYMINUTE/BYSECOND lists on an hourly rule) is noise, not a schedule.
MAX_OCCURRENCES_PER_SERIES = 200

#: Most steps one series is walked. With the walk starting near the window, an ordinary series
#: needs a few dozen; a monthly one from 1990 a few hundred.
MAX_SERIES_STEPS = 20_000

#: Wall-clock budget for expanding the series of one feed. Spent, the remaining series are skipped
#: (and the feed is marked truncated); single appointments are read regardless.
MAX_FEED_SECONDS = 3.0

#: Most occurrences kept from one feed (the earliest win). A busy week is a few dozen.
MAX_EVENTS_PER_FEED = 500


class FeedTooLargeError(ValueError):
    pass


@dataclass
class _Budget:
    """What expanding one feed may still spend, and whether anything had to be cut."""

    deadline: float
    truncated: bool = False

    def spent(self) -> bool:
        return monotonic() >= self.deadline


@dataclass(frozen=True, slots=True)
class ParsedFeed:
    events: list[CalendarEvent]
    #: Something was cut: a series capped or skipped, or the feed over `MAX_EVENTS_PER_FEED`.
    truncated: bool = False


def _text(component: icalendar.cal.Component, key: str) -> str:
    value = component.get(key)
    if value is None:
        return ""
    plain = "".join(ch for ch in str(value) if ch.isprintable() or ch.isspace())
    return " ".join(plain.split())[:MAX_TEXT_CHARS]


def _as_local(value: date | datetime, tz: tzinfo) -> datetime:
    """A datetime in `tz`: floating times are read as local, dates as local midnight."""
    if isinstance(value, datetime):
        return value.astimezone(tz) if value.tzinfo is not None else value.replace(tzinfo=tz)
    return datetime.combine(value, time.min, tzinfo=tz)


def _duration(component: icalendar.cal.Component, start: date | datetime) -> timedelta:
    end = component.get("DTEND")
    if end is not None:
        end_value = end.dt
        if isinstance(start, datetime) == isinstance(end_value, datetime):
            delta: timedelta = end_value - start
            return delta
    duration = component.get("DURATION")
    if duration is not None:
        declared: timedelta = duration.dt
        return declared
    # RFC 5545: no DTEND and no DURATION means one day for a date, an instant for a date-time.
    return timedelta(days=1) if not isinstance(start, datetime) else timedelta(0)


def _listed(component: icalendar.cal.Component, key: str) -> list[date | datetime]:
    """EXDATE/RDATE values, which `icalendar` hands back as one list or a list of lists."""
    raw = component.get(key)
    if raw is None:
        return []
    chunks = raw if isinstance(raw, list) else [raw]
    return [entry.dt for chunk in chunks for entry in chunk.dts]


def _ints(rule: Any, key: str) -> list[int]:
    return [int(value) for value in rule.get(key) or []]


def _impossible(rule: Any) -> bool:
    """A BYMONTH/BYMONTHDAY combination no month can satisfy, e.g. BYMONTH=2;BYMONTHDAY=31."""
    days = _ints(rule, "BYMONTHDAY")
    if not days:
        return False
    months = _ints(rule, "BYMONTH") or list(range(1, 13))
    return not any(
        0 < abs(day) <= _MONTH_DAYS[month - 1]
        for month in months
        if 1 <= month <= 12
        for day in days
    )


def _shifted(value: datetime, years: int) -> datetime:
    """`value` moved by whole 400-year cycles (a 29 February stays valid)."""
    return value.replace(year=value.year + years)


def _expand_rule(
    raw_rule: Any, naive_start: datetime, lo: datetime, hi: datetime, tz: tzinfo, budget: _Budget
) -> list[datetime]:
    """The starts of one RRULE inside `[lo, hi]` (naive wall time), bounded in every direction."""
    rule: Any = icalendar.vRecur(raw_rule)
    if _impossible(rule):
        log.info("calendar.series_impossible")
        return []
    raw_count = rule.pop("COUNT", None)
    count = int(raw_count[0]) if raw_count else None
    raw_until = rule.pop("UNTIL", None)
    # UNTIL is usually UTC ("...T080000Z"). Expanded in wall time, it has to be wall time too, or
    # the last occurrence of a 09:00 CET series ending "08:00Z" is dropped. Nothing after the
    # window is needed, so every series - counted ones too, see below - stops there.
    until = hi if not raw_until else min(_as_local(raw_until[0], tz).replace(tzinfo=None), hi)
    first = naive_start
    freq = str((rule.get("FREQ") or [""])[0]).upper()
    period = _FIXED_PERIODS.get(freq)
    if count is None and period is not None and first < lo:
        step = period * max(1, int((rule.get("INTERVAL") or [1])[0]))
        first += step * ((lo - first) // step)
    shift = _CYCLE_YEARS * ((_LAST_YEAR - hi.year) // _CYCLE_YEARS)
    parsed = rrulestr(rule.to_ical().decode(), dtstart=_shifted(first, shift), ignoretz=True)
    if not isinstance(parsed, rrule):  # pragma: no cover - one RRULE line is one rule
        return []
    # COUNT and UNTIL must not be combined, so COUNT becomes the step limit of the walk: the rule
    # alone is walked (RDATE/EXDATE are applied by the caller), exactly as COUNT counts.
    series = parsed.replace(until=_shifted(until, shift), count=None)
    limit = MAX_SERIES_STEPS if count is None else min(count, MAX_SERIES_STEPS)
    lo_s = _shifted(lo, shift)
    found: list[datetime] = []
    steps = 0
    for occurrence in itertools.islice(series, limit):
        steps += 1
        if occurrence >= lo_s:
            found.append(_shifted(occurrence, -shift))
        if len(found) > MAX_OCCURRENCES_PER_SERIES or (steps % 256 == 0 and budget.spent()):
            budget.truncated = True
            log.warning("calendar.series_capped", occurrences=len(found), steps=steps)
            break
    else:
        if steps == MAX_SERIES_STEPS and limit == MAX_SERIES_STEPS:
            budget.truncated = True
            log.warning("calendar.series_capped", occurrences=len(found), steps=steps)
    return found


def _occurrences(
    component: icalendar.cal.Component,
    start: date | datetime,
    window_start: datetime,
    window_end: datetime,
    tz: tzinfo,
    budget: _Budget | None = None,
) -> list[datetime]:
    """Every start of this event inside the window, expanded from RRULE/RDATE minus EXDATE.

    `dateutil` insists that DTSTART and UNTIL agree on being timezone-aware. Feeds do not always
    oblige (an all-day series with a UTC UNTIL is common), so the series is expanded in naive
    local time and made aware afterwards - the one representation every feed can be mapped to.
    """
    local_start = _as_local(start, tz)
    naive_start = local_start.replace(tzinfo=None)
    rule = component.get("RRULE")
    if rule is None and not component.get("RDATE"):
        return [local_start]
    lo = window_start.astimezone(tz).replace(tzinfo=None)
    hi = window_end.astimezone(tz).replace(tzinfo=None)
    budget = budget or _Budget(monotonic() + MAX_FEED_SECONDS)
    freq = str((rule.get("FREQ") or [""])[0]).upper() if rule is not None else ""
    if freq in IGNORED_FREQUENCIES:
        log.info("calendar.series_ignored", freq=freq)
        rule = None
    found: set[datetime] = set()
    if rule is not None:
        found.update(_expand_rule(rule, naive_start, lo, hi, tz, budget))
    elif lo <= naive_start <= hi:
        found.add(naive_start)
    for extra in _listed(component, "RDATE"):
        moment = _as_local(extra, tz).replace(tzinfo=None)
        if lo <= moment <= hi:
            found.add(moment)
    for excluded in _listed(component, "EXDATE"):
        found.discard(_as_local(excluded, tz).replace(tzinfo=None))
    ordered = sorted(found)
    if len(ordered) > MAX_OCCURRENCES_PER_SERIES:
        budget.truncated = True
        del ordered[MAX_OCCURRENCES_PER_SERIES:]
    return [occurrence.replace(tzinfo=tz) for occurrence in ordered]


def _cost_rank(component: icalendar.cal.Component) -> int:
    """0: a single occurrence (read first, always); 1: an ordinary series; 2: a sub-daily one."""
    rule = component.get("RRULE")
    if rule is None or component.get("RECURRENCE-ID") is not None:
        return 0
    freq = str((rule.get("FREQ") or [""])[0]).upper()
    dense = freq == "HOURLY" or any(rule.get(key) for key in ("BYHOUR", "BYMINUTE", "BYSECOND"))
    return 2 if dense else 1


def parse_ics(
    text: str,
    *,
    source: str,
    window_start: datetime,
    window_end: datetime,
    tz: tzinfo,
) -> list[CalendarEvent]:
    """The occurrences in `text` that overlap `[window_start, window_end)`, sorted by start.

    Cancelled events are dropped. An instance a feed rewrote (RECURRENCE-ID) replaces the one the
    series would have produced. A component the library cannot read is skipped, not fatal: one
    odd entry in a school timetable must not hide the rest of the week.
    """
    return parse_feed(
        text, source=source, window_start=window_start, window_end=window_end, tz=tz
    ).events


def parse_feed(
    text: str,
    *,
    source: str,
    window_start: datetime,
    window_end: datetime,
    tz: tzinfo,
    max_seconds: float = MAX_FEED_SECONDS,
) -> ParsedFeed:
    """`parse_ics`, and whether the feed had to be cut to stay within its bounds."""
    calendar = icalendar.Calendar.from_ical(text)
    components = list(calendar.walk("VEVENT"))
    overrides: set[tuple[str, datetime]] = set()
    for component in components:
        recurrence_id = component.get("RECURRENCE-ID")
        if recurrence_id is not None:
            overrides.add((_text(component, "UID"), _as_local(recurrence_id.dt, tz)))
    # The window is widened by a day so an occurrence that started yesterday and runs into
    # today is still found; the overlap test below does the exact cut.
    lo, hi = window_start - timedelta(days=1), window_end + timedelta(days=1)
    budget = _Budget(monotonic() + max_seconds)
    events: list[CalendarEvent] = []
    skipped = 0
    # Single appointments first and always; then ordinary series; dense ones last. A hostile series
    # can only use up what is left after the user's own appointments were read.
    for component in sorted(components, key=_cost_rank):
        if _cost_rank(component) > 0 and budget.spent():
            skipped += 1
            continue
        try:
            events.extend(_expand(component, source, lo, hi, tz, overrides, budget))
        except Exception as exc:  # noqa: BLE001 - one unreadable entry never hides the rest
            log.warning("calendar.event_skipped", source=source, error=type(exc).__name__)
    if skipped:
        budget.truncated = True
        log.warning("calendar.series_skipped", source=source, skipped=skipped)
    kept = [e for e in events if e.start < window_end and e.end > window_start]
    kept.sort(key=lambda e: (e.start, e.title))
    if len(kept) > MAX_EVENTS_PER_FEED:
        budget.truncated = True
        log.warning("calendar.feed_capped", source=source, events=len(kept))
        del kept[MAX_EVENTS_PER_FEED:]
    return ParsedFeed(events=kept, truncated=budget.truncated)


def _expand(
    component: icalendar.cal.Component,
    source: str,
    lo: datetime,
    hi: datetime,
    tz: tzinfo,
    overrides: set[tuple[str, datetime]],
    budget: _Budget,
) -> list[CalendarEvent]:
    if _text(component, "STATUS").upper() == "CANCELLED":
        return []
    dtstart = component.get("DTSTART")
    if dtstart is None:
        return []
    start_value = dtstart.dt
    all_day = not isinstance(start_value, datetime)
    duration = _duration(component, start_value)
    title = _text(component, "SUMMARY") or "(ohne Titel)"
    location = _text(component, "LOCATION")
    uid = _text(component, "UID")
    is_override = component.get("RECURRENCE-ID") is not None
    starts = (
        [_as_local(start_value, tz)]
        if is_override
        else _occurrences(component, start_value, lo - duration, hi, tz, budget)
    )
    return [
        CalendarEvent(
            start=start,
            end=start + duration,
            title=title,
            location=location,
            all_day=all_day,
            source=source,
        )
        for start in starts
        if is_override or (uid, start) not in overrides
    ]


# ---- the service ---------------------------------------------------------------------------------


@dataclass
class _Feed:
    source: CalendarSource
    events: list[CalendarEvent] = field(default_factory=list)
    fetched_at: datetime | None = None
    #: Last fetch, successful or not; what `is_stale` looks at, so a feed that keeps failing is
    #: retried on the interval and not on every question.
    attempted_at: datetime | None = None
    error: str = ""
    configured: bool = False
    truncated: bool = False


def _safe_error(exc: BaseException) -> str:
    """An error line that can be shown and logged: type and status, never the URL."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, PermissionError):
        return "host not in the active profile's egress allow-list"
    if isinstance(exc, FeedTooLargeError):
        return f"feed larger than {MAX_FEED_BYTES // (1024 * 1024)} MB"
    if isinstance(exc, ValueError):
        return "unreadable feed"  # the parser's message can quote feed content; it stays out
    return type(exc).__name__


def _local_midnight(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _zone_of(now: datetime) -> tzinfo:
    """The zone to expand a feed in. A fixed offset (what `datetime.now().astimezone()` carries)
    is today's offset, not a zone: it would put every occurrence after the next DST change an hour
    off. Such a clock is read as "the machine's local zone", which `dateutil` resolves per date."""
    zone = now.tzinfo
    if zone is None or isinstance(zone, timezone):
        local: tzinfo = dateutil_tz.tzlocal()
        return local
    return zone


class CalendarService:
    """Fetches the configured feeds, caches their occurrences, answers "what is on today".

    The credential store is only read by `refresh` (in a worker thread): whether a feed is
    configured is cached with its events, so answering a question never blocks the event loop on
    the keyring. A feed without a URL is re-checked every `UNCONFIGURED_RECHECK_S`, so a URL the
    user just pasted shows up within a minute.
    """

    #: How soon a feed without a stored URL is looked at again.
    UNCONFIGURED_RECHECK_S = 60.0

    def __init__(
        self,
        sources: list[CalendarSource],
        *,
        secret: Callable[[str], str | None],
        client_factory: Callable[..., httpx.AsyncClient],
        refresh_s: float = 900.0,
        lookahead_days: int = 7,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._feeds = {s.id: _Feed(source=s) for s in sources}
        self._secret = secret
        self._client_factory = client_factory
        self._refresh_s = refresh_s
        self._lookahead_days = lookahead_days
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._refreshing = asyncio.Lock()

    # ---- what is known ---------------------------------------------------------------------------

    @property
    def configured(self) -> bool:
        """Whether at least one feed had a URL stored at the last refresh."""
        return any(f.configured for f in self._feeds.values())

    def status(self) -> list[FeedStatus]:
        return [
            FeedStatus(
                id=f.source.id,
                label=f.source.label,
                configured=f.configured,
                fetched_at=f.fetched_at,
                error=f.error,
                count=len(f.events),
                truncated=f.truncated,
            )
            for f in self._feeds.values()
        ]

    def events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        found = [
            e for f in self._feeds.values() for e in f.events if e.start < end and e.end > start
        ]
        found.sort(key=lambda e: (e.start, e.title))
        return found

    def today(self) -> list[CalendarEvent]:
        start = _local_midnight(self._clock())
        return self.events(start, start + timedelta(days=1))

    def upcoming(self, days: int) -> list[CalendarEvent]:
        start = _local_midnight(self._clock())
        return self.events(start, start + timedelta(days=max(1, days)))

    def is_stale(self) -> bool:
        now = self._clock()
        return any(self._feed_stale(f, now) for f in self._feeds.values())

    def _feed_stale(self, feed: _Feed, now: datetime) -> bool:
        if feed.attempted_at is None:
            return True
        interval = self._refresh_s if feed.configured else self.UNCONFIGURED_RECHECK_S
        return (now - feed.attempted_at).total_seconds() > interval

    # ---- fetching --------------------------------------------------------------------------------

    async def refresh(self, *, only_stale: bool = False) -> None:
        """Fetch every configured feed once (or only the stale ones); failures are recorded per
        feed, never raised."""
        async with self._refreshing:
            now = self._clock()
            feeds = [f for f in self._feeds.values() if not only_stale or self._feed_stale(f, now)]
            await asyncio.gather(*(self._refresh_one(f) for f in feeds))

    async def ensure_fresh(self, timeout_s: float = 8.0) -> None:
        """Refresh if the cache is older than the refresh interval, but never wait longer than
        `timeout_s` - a tool answer built from a quarter-hour-old cache beats no answer."""
        if not self.is_stale():
            return
        try:
            await asyncio.wait_for(self.refresh(only_stale=True), timeout_s)
        except TimeoutError:
            log.info("calendar.refresh_slow", timeout_s=timeout_s)

    async def run(self) -> None:
        """Background loop: refresh now, then every `refresh_s`."""
        while True:
            await self.refresh()
            await asyncio.sleep(self._refresh_s)

    async def _refresh_one(self, feed: _Feed) -> None:
        now = self._clock()
        feed.attempted_at = now
        try:
            url = await asyncio.to_thread(self._secret, feed.source.secret)
        except Exception as exc:  # noqa: BLE001 - an unreachable keyring is reported, not "no feed"
            feed.error = f"credential store unavailable ({type(exc).__name__})"
            log.warning("calendar.secret_read_failed", source=feed.source.id, error=feed.error)
            return
        feed.configured = bool(url)
        if not url:
            feed.events, feed.error = [], ""
            return
        tz = _zone_of(now)
        window_start = _local_midnight(now)
        window_end = window_start + timedelta(days=self._lookahead_days + 1)
        try:
            text = await self._fetch(url)
            parsed = await asyncio.to_thread(
                parse_feed,
                text,
                source=feed.source.id,
                window_start=window_start,
                window_end=window_end,
                tz=tz,
            )
        except Exception as exc:  # noqa: BLE001 - recorded per feed; the loop and the tools go on
            feed.error = _safe_error(exc)
            log.warning("calendar.fetch_failed", source=feed.source.id, error=feed.error)
            return
        feed.events, feed.truncated = parsed.events, parsed.truncated
        feed.fetched_at, feed.error = now, ""
        log.info(
            "calendar.fetched",
            source=feed.source.id,
            events=len(feed.events),
            truncated=feed.truncated,
        )

    async def _fetch(self, url: str) -> str:
        kwargs: dict[str, Any] = {"timeout": httpx.Timeout(20.0, connect=5.0)}
        chunks: list[bytes] = []
        size = 0
        async with (
            self._client_factory(**kwargs) as client,
            client.stream("GET", url, follow_redirects=True) as response,
        ):
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_FEED_BYTES:
                    raise FeedTooLargeError(size)
                chunks.append(chunk)
            encoding = response.encoding or "utf-8"
        return b"".join(chunks).decode(encoding, errors="replace")

"""Calendar feeds: the user's appointments from iCalendar subscriptions.

A feed is a private `.ics` URL - Google Calendar's "secret address in iCal format", a school
timetable's export. The URL *is* the credential, so it lives in the Windows Credential Manager
like a token and is never written to a log, a prompt or an error message: when a fetch fails,
only the exception type and the HTTP status are kept.

Parsing is `icalendar` for the format and `dateutil.rrule` for recurrence (both permissively
licensed; the usual `recurring-ical-events` is LGPL). Occurrences are expanded into a bounded
window around today, which is all a companion needs and keeps a ten-year weekly series cheap.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any

import httpx
import icalendar
from dateutil.rrule import rruleset, rrulestr
from pydantic import BaseModel, ConfigDict

from nox.core.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "CalendarEvent",
    "CalendarService",
    "CalendarSource",
    "FeedStatus",
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


# ---- parsing -------------------------------------------------------------------------------------


def _text(component: icalendar.cal.Component, key: str) -> str:
    value = component.get(key)
    return "" if value is None else " ".join(str(value).split())


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


def _occurrences(
    component: icalendar.cal.Component,
    start: date | datetime,
    window_start: datetime,
    window_end: datetime,
    tz: tzinfo,
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
    series = rruleset()
    if rule is not None:
        text = rule.to_ical().decode()
        series.rrule(rrulestr(text, dtstart=naive_start, ignoretz=True))
    else:
        series.rdate(naive_start)
    for extra in _listed(component, "RDATE"):
        series.rdate(_as_local(extra, tz).replace(tzinfo=None))
    for excluded in _listed(component, "EXDATE"):
        series.exdate(_as_local(excluded, tz).replace(tzinfo=None))
    lo = window_start.astimezone(tz).replace(tzinfo=None)
    hi = window_end.astimezone(tz).replace(tzinfo=None)
    return [occurrence.replace(tzinfo=tz) for occurrence in series.between(lo, hi, inc=True)]


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
    events: list[CalendarEvent] = []
    for component in components:
        try:
            events.extend(_expand(component, source, lo, hi, tz, overrides))
        except Exception as exc:  # noqa: BLE001 - one unreadable entry never hides the rest
            log.warning("calendar.event_skipped", source=source, error=type(exc).__name__)
    kept = [e for e in events if e.start < window_end and e.end > window_start]
    kept.sort(key=lambda e: (e.start, e.title))
    return kept


def _expand(
    component: icalendar.cal.Component,
    source: str,
    lo: datetime,
    hi: datetime,
    tz: tzinfo,
    overrides: set[tuple[str, datetime]],
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
        else _occurrences(component, start_value, lo, hi, tz)
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
    error: str = ""
    configured: bool = False


def _safe_error(exc: BaseException) -> str:
    """An error line that can be shown and logged: type and status, never the URL."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, PermissionError):
        return "host not in the active profile's egress allow-list"
    return type(exc).__name__


def _local_midnight(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


class CalendarService:
    """Fetches the configured feeds, caches their occurrences, answers "what is on today"."""

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
        """Whether at least one feed has a URL stored."""
        return any(self._secret(f.source.secret) for f in self._feeds.values())

    def status(self) -> list[FeedStatus]:
        return [
            FeedStatus(
                id=f.source.id,
                label=f.source.label,
                configured=f.configured or bool(self._secret(f.source.secret)),
                fetched_at=f.fetched_at,
                error=f.error,
                count=len(f.events),
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
        return any(
            f.fetched_at is None or (now - f.fetched_at).total_seconds() > self._refresh_s
            for f in self._feeds.values()
            if self._secret(f.source.secret)
        )

    # ---- fetching --------------------------------------------------------------------------------

    async def refresh(self) -> None:
        """Fetch every configured feed once; failures are recorded per feed, never raised."""
        async with self._refreshing:
            await asyncio.gather(*(self._refresh_one(f) for f in self._feeds.values()))

    async def ensure_fresh(self, timeout_s: float = 8.0) -> None:
        """Refresh if the cache is older than the refresh interval, but never wait longer than
        `timeout_s` - a tool answer built from a quarter-hour-old cache beats no answer."""
        if not self.is_stale():
            return
        try:
            await asyncio.wait_for(self.refresh(), timeout_s)
        except TimeoutError:
            log.info("calendar.refresh_slow", timeout_s=timeout_s)

    async def run(self) -> None:
        """Background loop: refresh now, then every `refresh_s`."""
        while True:
            await self.refresh()
            await asyncio.sleep(self._refresh_s)

    async def _refresh_one(self, feed: _Feed) -> None:
        url = self._secret(feed.source.secret)
        feed.configured = bool(url)
        if not url:
            feed.events, feed.error = [], ""
            return
        now = self._clock()
        tz = now.tzinfo or datetime.now().astimezone().tzinfo
        window_start = _local_midnight(now)
        window_end = window_start + timedelta(days=self._lookahead_days + 1)
        try:
            text = await self._fetch(url)
            feed.events = await asyncio.to_thread(
                parse_ics,
                text,
                source=feed.source.id,
                window_start=window_start,
                window_end=window_end,
                tz=tz,  # type: ignore[arg-type]
            )
        except Exception as exc:  # noqa: BLE001 - recorded per feed; the loop and the tools go on
            feed.error = _safe_error(exc)
            log.warning("calendar.fetch_failed", source=feed.source.id, error=feed.error)
            return
        feed.fetched_at, feed.error = now, ""
        log.info("calendar.fetched", source=feed.source.id, events=len(feed.events))

    async def _fetch(self, url: str) -> str:
        kwargs: dict[str, Any] = {"timeout": httpx.Timeout(20.0, connect=5.0)}
        async with self._client_factory(**kwargs) as client:
            response = await client.get(url, follow_redirects=True)
            response.raise_for_status()
            return response.text

"""`nox.day.calendar`: feeds become the user's appointments, and nothing about a feed URL leaks."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from nox.day.calendar import CalendarService, CalendarSource, parse_ics

TZ = ZoneInfo("Europe/Berlin")
WEEK_START = datetime(2026, 10, 12, tzinfo=TZ)  # a Monday
WEEK_END = WEEK_START + timedelta(days=7)

FEED = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:dentist
DTSTART:20261015T090000Z
DTEND:20261015T093000Z
SUMMARY:Zahnarzt
LOCATION:Musterstadt
END:VEVENT
BEGIN:VEVENT
UID:holiday
DTSTART;VALUE=DATE:20261016
SUMMARY:Brueckentag
END:VEVENT
BEGIN:VEVENT
UID:school
DTSTART:20261001T080000Z
DTEND:20261001T084500Z
RRULE:FREQ=WEEKLY;BYDAY=TH;COUNT=20
EXDATE:20261022T080000Z
SUMMARY:Mathe
END:VEVENT
BEGIN:VEVENT
UID:school
RECURRENCE-ID:20261015T080000Z
DTSTART:20261015T100000Z
DTEND:20261015T104500Z
SUMMARY:Mathe (verlegt)
END:VEVENT
BEGIN:VEVENT
UID:gone
DTSTART:20261014T120000Z
DTEND:20261014T130000Z
STATUS:CANCELLED
SUMMARY:Abgesagt
END:VEVENT
END:VCALENDAR
"""


def _titles(window_start: datetime, window_end: datetime) -> list[tuple[str, str]]:
    events = parse_ics(FEED, source="t", window_start=window_start, window_end=window_end, tz=TZ)
    return [(e.start.strftime("%a %H:%M"), e.title) for e in events]


def test_a_feed_becomes_local_appointments_in_order() -> None:
    assert _titles(WEEK_START, WEEK_END) == [
        ("Thu 11:00", "Zahnarzt"),
        ("Thu 12:00", "Mathe (verlegt)"),
        ("Fri 00:00", "Brueckentag"),
    ]


def test_all_day_events_span_a_day_and_cancelled_ones_vanish() -> None:
    events = parse_ics(FEED, source="t", window_start=WEEK_START, window_end=WEEK_END, tz=TZ)
    holiday = next(e for e in events if e.title == "Brueckentag")
    assert holiday.all_day and holiday.end - holiday.start == timedelta(days=1)
    assert all(e.title != "Abgesagt" for e in events)
    assert next(e for e in events if e.title == "Zahnarzt").location == "Musterstadt"


def test_recurrence_honours_exdate_and_the_override_replaces_its_instance() -> None:
    later = _titles(WEEK_END, WEEK_END + timedelta(days=14))
    # 22 Oct is excluded, 29 Oct is a normal instance; the moved 15 Oct lesson is above.
    assert later == [("Thu 10:00", "Mathe")]
    assert all(t != "Mathe" for _, t in _titles(WEEK_START, WEEK_END))


class _FakeClient:
    def __init__(self, outcome: Any) -> None:
        self._outcome = outcome

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def stream(self, method: str, url: str, **_kwargs: Any) -> Any:
        return _FakeStream(self._outcome, httpx.Request(method, url))


class _FakeStream:
    def __init__(self, outcome: Any, request: httpx.Request) -> None:
        self._outcome = outcome
        self._request = request

    async def __aenter__(self) -> Any:
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return httpx.Response(200, text=self._outcome, request=self._request)

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _service(outcome: Any, *, configured: bool = True) -> CalendarService:
    secret_url = "https://calendar.example/private/SECRET-TOKEN/basic.ics"
    return CalendarService(
        [CalendarSource("google", "Google Kalender", "nox/calendar/google_ics_url")],
        secret=lambda _name: secret_url if configured else None,
        client_factory=lambda **_kw: _FakeClient(outcome),
        clock=lambda: datetime(2026, 10, 15, 8, 0, tzinfo=TZ),
    )


async def test_the_service_answers_today_from_a_fetched_feed() -> None:
    service = _service(FEED)
    assert service.is_stale()
    await service.refresh()
    assert [e.title for e in service.today()] == ["Zahnarzt", "Mathe (verlegt)"]
    assert [e.title for e in service.upcoming(2)][-1] == "Brueckentag"
    status = service.status()[0]
    assert status.configured and status.error == "" and status.count >= 3
    assert not service.is_stale()


async def test_an_unconfigured_feed_is_not_fetched_and_says_so() -> None:
    service = _service(FEED, configured=False)
    await service.refresh()
    assert not service.configured and service.today() == []
    assert service.status()[0].configured is False


async def test_a_failed_fetch_never_reveals_the_url() -> None:
    request = httpx.Request("GET", "https://calendar.example/private/SECRET-TOKEN/basic.ics")
    failure = httpx.HTTPStatusError("boom", request=request, response=httpx.Response(404))
    service = _service(failure)
    await service.refresh()
    status = service.status()[0]
    assert status.error == "HTTP 404" and "SECRET" not in status.error
    denied = _service(PermissionError("egress denied https://calendar.example/private/SECRET"))
    await denied.refresh()
    assert "SECRET" not in denied.status()[0].error and "allow-list" in denied.status()[0].error


DST_FEED = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:standup
DTSTART;TZID=Europe/Berlin:20261001T090000
DTEND;TZID=Europe/Berlin:20261001T091500
RRULE:FREQ=WEEKLY;BYDAY=TH;UNTIL=20261105T080000Z
SUMMARY:Standup
END:VEVENT
END:VCALENDAR
"""


def test_a_weekly_series_keeps_its_wall_clock_time_across_the_dst_change() -> None:
    # 25 Oct 2026 is the switch from CEST to CET; 09:00 must stay 09:00 on both sides, and the
    # UTC UNTIL ("08:00Z" = 09:00 CET) must keep the last occurrence on 5 Nov.
    start = datetime(2026, 10, 19, tzinfo=TZ)
    events = parse_ics(
        DST_FEED, source="t", window_start=start, window_end=start + timedelta(days=21), tz=TZ
    )
    assert [(e.start.strftime("%d.%m %H:%M"), e.start.utcoffset()) for e in events] == [
        ("22.10 09:00", timedelta(hours=2)),
        ("29.10 09:00", timedelta(hours=1)),
        ("05.11 09:00", timedelta(hours=1)),
    ]


def test_a_fixed_offset_clock_is_read_as_the_machine_zone() -> None:
    from datetime import timezone

    from dateutil import tz as dateutil_tz

    from nox.day.calendar import _zone_of

    assert isinstance(
        _zone_of(datetime(2026, 10, 15, tzinfo=timezone(timedelta(hours=2)))), dateutil_tz.tzlocal
    )
    assert _zone_of(datetime(2026, 10, 15, tzinfo=TZ)) is TZ


async def test_a_failing_feed_is_retried_on_the_interval_not_on_every_question() -> None:
    service = _service(PermissionError("denied"))
    await service.refresh()
    assert service.status()[0].error and not service.is_stale()


def _series(rule: str, *, dtstart: str = "20261015T080000") -> str:
    return (
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//EN\nBEGIN:VEVENT\nUID:flood\n"
        f"DTSTART;TZID=Europe/Berlin:{dtstart}\nDURATION:PT1S\nRRULE:{rule}\nSUMMARY:Flood\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )


@pytest.mark.parametrize("freq", ["SECONDLY", "MINUTELY"])
def test_a_sub_hourly_series_is_read_as_its_first_occurrence_only(freq: str) -> None:
    # A stranger's invite lands in a Google feed unasked; FREQ=SECONDLY expanded over the lookahead
    # window would be hundreds of thousands of events in one tool result.
    start = datetime(2026, 10, 15, tzinfo=TZ)
    events = parse_ics(
        _series(f"FREQ={freq}"),
        source="t",
        window_start=start,
        window_end=start + timedelta(days=8),
        tz=TZ,
    )
    assert [e.start.strftime("%d.%m %H:%M:%S") for e in events] == ["15.10 08:00:00"]


def test_an_hourly_series_that_expands_to_every_second_is_capped_per_series() -> None:
    from nox.day.calendar import MAX_OCCURRENCES_PER_SERIES

    every_second = ",".join(str(n) for n in range(60))
    start = datetime(2026, 10, 15, tzinfo=TZ)
    events = parse_ics(
        _series(f"FREQ=HOURLY;BYMINUTE={every_second};BYSECOND={every_second}"),
        source="t",
        window_start=start,
        window_end=start + timedelta(days=8),
        tz=TZ,
    )
    assert len(events) == MAX_OCCURRENCES_PER_SERIES
    assert events[0].start == datetime(2026, 10, 15, 8, 0, tzinfo=TZ)


def test_an_old_daily_series_without_until_still_reaches_the_window() -> None:
    # The iteration bound must not cut off an ordinary long-running series before today.
    start = datetime(2026, 10, 15, tzinfo=TZ)
    events = parse_ics(
        _series("FREQ=DAILY", dtstart="19900101T070000"),
        source="t",
        window_start=start,
        window_end=start + timedelta(days=1),
        tz=TZ,
    )
    assert [e.start for e in events] == [datetime(2026, 10, 15, 7, 0, tzinfo=TZ)]


def test_a_feed_with_too_many_events_keeps_only_the_earliest() -> None:
    from nox.day.calendar import MAX_EVENTS_PER_FEED

    body = "".join(
        f"BEGIN:VEVENT\nUID:e{n}\nDTSTART;TZID=Europe/Berlin:20261015T{8 + n // 3600:02d}"
        f"{n // 60 % 60:02d}{n % 60:02d}\nDURATION:PT1M\nSUMMARY:E{n}\nEND:VEVENT\n"
        for n in range(MAX_EVENTS_PER_FEED + 50)
    )
    text = f"BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//EN\n{body}END:VCALENDAR\n"
    start = datetime(2026, 10, 15, tzinfo=TZ)
    events = parse_ics(
        text, source="t", window_start=start, window_end=start + timedelta(days=1), tz=TZ
    )
    assert len(events) == MAX_EVENTS_PER_FEED
    assert events[0].title == "E0" and events[-1].title == f"E{MAX_EVENTS_PER_FEED - 1}"

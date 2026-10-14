"""`nox.day.calendar`: feeds become the user's appointments, and nothing about a feed URL leaks."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

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
LOCATION:Witten
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
    assert next(e for e in events if e.title == "Zahnarzt").location == "Witten"


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

    async def get(self, url: str, **_kwargs: Any) -> Any:
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return httpx.Response(200, text=self._outcome, request=httpx.Request("GET", url))


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

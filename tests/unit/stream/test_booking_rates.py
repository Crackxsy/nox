"""`FunkenBooking` turns the plugin's rate-less requests into real amounts, and the new
`stream.twitch` settings (`!streamplan` text, ignored bots) reject what could break an IRC line."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from nox.core.config import FunkenConfig, StreamTwitchConfig
from nox.core.events import E, Event
from nox.stream.booking import FunkenBooking


class _Funken:
    """Just the two members `FunkenBooking._on_awarded` touches."""

    RATES = {"message": 1.0, "sub": 50.0, "bit": 0.1, "raid": 20.0}

    def __init__(self) -> None:
        self.earned: list[tuple[str, str, float | None]] = []

    def rate(self, reason: str) -> float:
        return self.RATES.get(reason, 0.0)

    async def earn(self, viewer_id: str, reason: str, *, amount: float | None = None) -> Any:
        self.earned.append((viewer_id, reason, amount))
        return SimpleNamespace(skipped=False, delta=amount or 0.0)


def _booking(funken: _Funken, *, active: bool = True, cap: float = 200.0) -> FunkenBooking:
    return FunkenBooking(
        bus=None,  # type: ignore[arg-type]
        funken=funken,  # type: ignore[arg-type]
        viewers=None,  # type: ignore[arg-type]
        executor=None,  # type: ignore[arg-type]
        config=FunkenConfig(earn_daily_cap=cap),
        is_session_active=lambda: active,
    )


def _award(**payload: Any) -> Event:
    return Event(name=E.STREAM_FUNKEN_AWARDED, payload=payload)


@pytest.mark.parametrize(
    ("reason", "units", "amount"),
    [
        ("message", 1.0, 1.0),
        ("sub", 1.0, 50.0),
        ("sub", 5.0, 200.0),  # exactly the daily cap
        ("bit", 100.0, pytest.approx(10.0)),
        ("raid", 1.0, 20.0),
    ],
)
async def test_zero_delta_means_the_configured_rate_times_units(
    reason: str, units: float, amount: Any
) -> None:
    funken = _Funken()
    await _booking(funken)._on_awarded(
        _award(viewer_id="42", delta=0.0, reason=reason, units=units)
    )
    assert funken.earned == [("42", reason, amount)]


async def test_an_explicit_delta_still_wins() -> None:
    funken = _Funken()
    await _booking(funken)._on_awarded(_award(viewer_id="42", delta=5.0, reason="rps_win"))
    assert funken.earned == [("42", "rps_win", 5.0)]


@pytest.mark.parametrize(
    "payload",
    [
        {"viewer_id": "42", "delta": 0.0, "reason": "mystery", "units": 1.0},  # no rate
        {"viewer_id": "42", "delta": 0.0, "reason": "bit", "units": 0.0},
        {"viewer_id": "42", "delta": 0.0, "reason": "bit", "units": -3.0},
        {"viewer_id": "", "delta": 0.0, "reason": "message", "units": 1.0},
    ],
)
async def test_worthless_requests_book_nothing(payload: dict[str, Any]) -> None:
    funken = _Funken()
    await _booking(funken)._on_awarded(_award(**payload))
    assert funken.earned == []


async def test_nothing_is_booked_outside_a_live_stream() -> None:
    funken = _Funken()
    await _booking(funken, active=False)._on_awarded(
        _award(viewer_id="42", delta=0.0, reason="message", units=1.0)
    )
    assert funken.earned == []


# -- stream.twitch settings -----------------------------------------------------------------------


def test_schedule_text_is_trimmed_and_kept() -> None:
    assert StreamTwitchConfig(schedule_text="  Mo, Mi, Fr ab 19 Uhr  ").schedule_text == (
        "Mo, Mi, Fr ab 19 Uhr"
    )


@pytest.mark.parametrize("bad", ["Mo\nPRIVMSG #x :boom", "Mo\rFr", "a\x00b", "x" * 301])
def test_schedule_text_cannot_break_an_irc_line(bad: str) -> None:
    with pytest.raises(ValidationError):
        StreamTwitchConfig(schedule_text=bad)


def test_ignored_chatters_are_normalised() -> None:
    config = StreamTwitchConfig(ignored_chatters=[" @Nightbot ", "", "Fossabot"])
    assert config.ignored_chatters == ["nightbot", "fossabot"]


def test_ignored_chatters_reject_non_logins() -> None:
    with pytest.raises(ValidationError):
        StreamTwitchConfig(ignored_chatters=["not a login"])


def test_the_common_chat_bots_are_ignored_by_default() -> None:
    defaults = StreamTwitchConfig().ignored_chatters
    for bot in ("nightbot", "streamelements", "moobot", "fossabot"):
        assert bot in defaults

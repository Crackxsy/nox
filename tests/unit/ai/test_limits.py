"""Plan limits: how one is recognised (stream event, error wording) and what it carries."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from nox.ai.limits import DEFAULT_LIMIT_RETRY_S, describe_limit, limit_for, looks_like_limit
from nox.ai.providers.claude_stream import ClaudeStreamParser, StreamItem

NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)


def feed(info: dict[str, object]) -> StreamItem | None:
    parser = ClaudeStreamParser()
    return parser.feed(json.dumps({"type": "rate_limit_event", "rate_limit_info": info}))


def test_an_allowed_status_is_not_a_refusal() -> None:
    item = feed({"status": "allowed", "resetsAt": 1, "rateLimitType": "five_hour"})

    assert item is not None and item.kind == "rate_limit"
    assert item.refused is False
    assert item.limit_kind == "" and item.limit_resets_at is None


def test_a_warning_is_not_a_refusal() -> None:
    item = feed({"status": "allowed_warning", "rateLimitType": "seven_day"})

    assert item is not None and item.refused is False


def test_a_rejected_status_carries_the_window_and_its_reset() -> None:
    item = feed({"status": "rejected", "resetsAt": 1_800_000_000, "rateLimitType": "five_hour"})

    assert item is not None and item.refused
    assert item.limit_kind == "five_hour"
    assert item.limit_resets_at == 1_800_000_000


def test_a_rejected_window_is_found_inside_the_unified_windows() -> None:
    item = feed(
        {
            "status": "allowed",
            "unifiedWindows": {
                "five_hour": {"status": "allowed", "resetsAt": 100},
                "seven_day": {"status": "rejected", "resetsAt": 500},
            },
        }
    )

    assert item is not None and item.refused
    assert (item.limit_kind, item.limit_resets_at) == ("seven_day", 500)


def test_when_several_windows_refuse_the_one_that_resets_last_counts() -> None:
    item = feed(
        {
            "status": "rejected",
            "rateLimitType": "five_hour",
            "resetsAt": 100,
            "unifiedWindows": {
                "five_hour": {"status": "rejected", "resetsAt": 100},
                "seven_day": {"status": "rejected", "resetsAt": 900},
            },
        }
    )

    assert item is not None
    assert (item.limit_kind, item.limit_resets_at) == ("seven_day", 900)


@pytest.mark.parametrize("info", [{}, {"status": 5}, {"unifiedWindows": "nonsense"}])
def test_a_malformed_status_is_not_a_refusal(info: dict[str, object]) -> None:
    item = feed(info)

    assert item is not None and item.refused is False


@pytest.mark.parametrize(
    "message",
    [
        "5-hour limit reached ∙ resets 3pm",
        "You've hit your limit · resets 11pm (Europe/Berlin)",
        "Claude usage limit reached. Your limit will reset at 4pm",
        "Weekly usage limit reached",
        "API Error: rate limit exceeded",
        "quota exceeded for this organisation",
    ],
)
def test_the_wording_of_a_plan_limit_is_recognised(message: str) -> None:
    assert looks_like_limit(message)


@pytest.mark.parametrize(
    "message",
    [
        "Not logged in · Please run /login",
        "no result line (exit code 1)",
        "overloaded",
        "the Claude Code session ended mid-turn",
        "",
    ],
)
def test_other_failures_are_not_mistaken_for_a_limit(message: str) -> None:
    assert not looks_like_limit(message)


def test_a_reset_in_the_future_is_taken_as_given() -> None:
    reset = NOW + timedelta(hours=3)

    limit = limit_for("claude_code", kind="five_hour", resets_at=reset.timestamp(), now=NOW)

    assert limit.until == reset
    assert limit.estimated is False
    assert limit.kind == "five_hour"
    assert limit.active(NOW) and not limit.active(reset)


@pytest.mark.parametrize("resets_at", [None, (NOW - timedelta(hours=1)).timestamp()])
def test_a_missing_or_past_reset_falls_back_to_the_default_delay(resets_at: float | None) -> None:
    limit = limit_for("claude_code", kind="", resets_at=resets_at, now=NOW)

    assert limit.until == NOW + timedelta(seconds=DEFAULT_LIMIT_RETRY_S)
    assert limit.estimated is True
    assert limit.kind == "unknown"


def test_the_status_line_says_which_limit_and_until_when() -> None:
    exact = limit_for("claude_code", kind="seven_day", resets_at=NOW.timestamp() + 7200, now=NOW)
    guess = limit_for("claude_code", kind="", resets_at=None, now=NOW)

    assert describe_limit(exact) == "usage limit reached (seven_day); resets 2026-03-10 14:00 UTC"
    assert describe_limit(guess) == (
        "usage limit reached (unknown); retrying after 2026-03-10 12:15 UTC"
    )

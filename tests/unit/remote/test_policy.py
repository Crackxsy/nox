"""`RemoteCommandPolicy` (ST-17-02/04/07): the decision table, in isolation. Every rule in the
module docstring of `nox.remote.policy` has at least one test here, and the denials are the point.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nox.core.events import RemoteMessage
from nox.remote.models import DeviceRow
from nox.remote.policy import (
    REMOTE_PRIVACY_MODES,
    RemoteCommandPolicy,
    RemoteRateLimiter,
    parse_command,
)


def device(*, last_update_id: int = 0) -> DeviceRow:
    return DeviceRow(
        id="dev-1",
        name="Pixel",
        paired_at=datetime(2026, 9, 14, tzinfo=UTC),
        last_update_id=last_update_id,
    )


def message(text: str, *, update_id: int = 1, sender_id: str = "987654321") -> RemoteMessage:
    return RemoteMessage(sender_id=sender_id, chat_id="77", update_id=update_id, text=text)


# -- parsing ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/status", ("status", [])),
        ("/privacy private", ("privacy", ["private"])),
        ("/privacy@noxbot offline", ("privacy", ["offline"])),
        ("/PAIR ABCD2345", ("pair", ["ABCD2345"])),
        ("/unpair", ("unpair", [])),
        ("wie geht es dir?", ("chat", ["wie geht es dir?"])),
        ("/nonsense", ("unknown", [])),
        ("/", ("unknown", [])),
    ],
)
def test_parse_command(text, expected):
    assert parse_command(text) == expected


def test_a_mistyped_command_is_never_silently_treated_as_chat():
    """A typo must not be forwarded to the AI as conversation - it is a named rejection."""
    assert parse_command("/statu")[0] == "unknown"


# -- unpaired senders ---------------------------------------------------------------------------


def test_unpaired_sender_may_only_pair(policy: RemoteCommandPolicy):
    assert policy.decide(message("/pair ABCD2345"), None).allowed


@pytest.mark.parametrize("text", ["/status", "/kill", "/privacy private", "/unpair", "hallo"])
def test_unpaired_sender_is_rejected_for_everything_else(policy: RemoteCommandPolicy, text):
    decision = policy.decide(message(text), None)

    assert not decision.allowed and decision.reason == "not_paired"


def test_paired_sender_cannot_pair_again(policy: RemoteCommandPolicy):
    decision = policy.decide(message("/pair ABCD2345"), device())

    assert not decision.allowed and decision.reason == "already_paired"


# -- replay -------------------------------------------------------------------------------------


def test_a_reused_update_id_is_rejected_as_replay(policy: RemoteCommandPolicy):
    decision = policy.decide(message("/kill", update_id=5), device(last_update_id=5))

    assert not decision.allowed and decision.reason == "replay"


def test_an_older_update_id_is_rejected_as_replay(policy: RemoteCommandPolicy):
    decision = policy.decide(message("/kill", update_id=4), device(last_update_id=5))

    assert not decision.allowed and decision.reason == "replay"


def test_a_newer_update_id_passes(policy: RemoteCommandPolicy):
    assert policy.decide(message("/kill", update_id=6), device(last_update_id=5)).allowed


# -- kill switch and resume ---------------------------------------------------------------------


def test_kill_is_allowed_from_a_paired_phone(policy: RemoteCommandPolicy):
    assert policy.decide(message("/kill"), device()).allowed


def test_resume_is_never_allowed_from_the_phone(policy: RemoteCommandPolicy):
    """Security Model §6 / OP-6 D: resume stays local-only. It is denied by name so the attempt is
    audited as such instead of looking like a typo."""
    decision = policy.decide(message("/resume"), device())

    assert not decision.allowed and decision.reason == "resume_not_remote"


# -- privacy asymmetry --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", sorted(REMOTE_PRIVACY_MODES))
def test_privacy_downgrade_is_allowed(policy: RemoteCommandPolicy, mode):
    assert policy.decide(message(f"/privacy {mode}"), device()).allowed


@pytest.mark.parametrize("mode", ["full", "balanced", "FULL", "anything"])
def test_privacy_upgrade_is_denied(policy: RemoteCommandPolicy, mode):
    decision = policy.decide(message(f"/privacy {mode}"), device())

    assert not decision.allowed and decision.reason == "privacy_upgrade_denied"


def test_privacy_without_a_mode_is_denied(policy: RemoteCommandPolicy):
    decision = policy.decide(message("/privacy"), device())

    assert not decision.allowed and decision.reason == "privacy_mode_missing"


# -- chat and rate limiting ---------------------------------------------------------------------


def test_chat_can_be_switched_off():
    policy = RemoteCommandPolicy(chat_enabled=False)

    decision = policy.decide(message("hallo"), device())

    assert not decision.allowed and decision.reason == "chat_disabled"


def test_rate_limit_is_per_sender_and_refills(ticker):
    policy = RemoteCommandPolicy(
        rate_limiter=RemoteRateLimiter(per_minute=60, burst=2, clock=ticker)
    )
    dev = device()

    first = policy.decide(message("/status", update_id=1), dev)
    second = policy.decide(message("/status", update_id=2), dev)
    third = policy.decide(message("/status", update_id=3), dev)

    assert first.allowed and second.allowed
    assert not third.allowed and third.reason == "rate_limited"
    ticker.advance(1.0)  # one token per second at 60/min
    assert policy.decide(message("/status", update_id=4), dev).allowed


def test_an_unpaired_flood_cannot_exhaust_a_paired_devices_budget(ticker):
    policy = RemoteCommandPolicy(
        rate_limiter=RemoteRateLimiter(per_minute=60, burst=1, clock=ticker)
    )

    assert policy.decide(message("/pair AAAA2345", sender_id="111"), None).allowed
    flooded = policy.decide(message("/pair AAAA2345", sender_id="111"), None)
    assert not flooded.allowed and flooded.reason == "rate_limited"
    assert policy.decide(message("/status", update_id=9), device()).allowed

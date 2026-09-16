"""`RemoteService` end to end over the real repository (ST-17-04/07, Spec v0.8 §9).

This is where the security claims of the release are asserted: an unpaired sender gets nothing and
is still audited, a kill from the phone engages without a PIN but can never be resumed from there,
the privacy switch is one-way, and every attempt lands in both audit trails.
"""

from __future__ import annotations

import pytest

from nox.core.events import E, Event, RemoteMessage
from nox.core.state import PrivacyMode
from nox.remote.policy import RemoteCommandPolicy, RemoteRateLimiter
from nox.remote.service import REMOTE_KILL_ORIGIN, RemoteService

from .conftest import FakeChannel, FakeKillSwitch, FakePrivacy

SENDER = "987654321"
STRANGER = "111222333"


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel()


@pytest.fixture
def killswitch() -> FakeKillSwitch:
    return FakeKillSwitch()


@pytest.fixture
def privacy() -> FakePrivacy:
    return FakePrivacy()


@pytest.fixture
def service(repo, pairing, killswitch, privacy, channel, bus, audit, clock):
    async def chat(text: str) -> str:
        return f"Antwort auf: {text}"

    return RemoteService(
        repo=repo,
        pairing=pairing,
        policy=RemoteCommandPolicy(rate_limiter=RemoteRateLimiter(per_minute=600, burst=50)),
        killswitch=killswitch,
        privacy=privacy,
        send=channel,
        status=lambda: {"Modus": "companion", "Privatsphäre": privacy.mode.value},
        chat=chat,
        bus=bus,
        audit=audit,
        clock=clock,
    )


def msg(text: str, *, sender_id: str = SENDER, update_id: int = 1) -> RemoteMessage:
    return RemoteMessage(sender_id=sender_id, chat_id="77", update_id=update_id, text=text)


async def pair(service, pairing, sender_id: str = SENDER) -> str:
    start = await pairing.start(device_name="Pixel")
    result = await service.handle(msg(f"/pair {start.code}", sender_id=sender_id))
    assert result.allowed
    return start.code


# -- unpaired senders ---------------------------------------------------------------------------


async def test_unpaired_sender_is_ignored_on_the_wire_but_audited(service, channel, db, audit):
    decision = await service.handle(msg("/kill", sender_id=STRANGER))

    assert not decision.allowed and decision.reason == "not_paired"
    assert channel.sent == []  # no answer at all: the bot never confirms that Nox is listening
    rows = db.fetch_all("SELECT command, decision, reason FROM remote_audit")
    assert [(r["command"], r["decision"], r["reason"]) for r in rows] == [
        ("kill", "deny", "not_paired")
    ]
    assert audit.entries[-1]["decision"] == "deny"


async def test_the_audit_row_holds_a_pseudonym_not_the_account_id(service, db):
    await service.handle(msg("/kill", sender_id=STRANGER))

    row = db.fetch_all("SELECT * FROM remote_audit")[0]

    assert STRANGER not in str(dict(row))
    assert len(str(row["sender_ref"])) == 12


async def test_an_unpaired_sender_cannot_kill(service, killswitch):
    await service.handle(msg("/kill", sender_id=STRANGER))

    assert not killswitch.engaged


# -- pairing through the bot --------------------------------------------------------------------


async def test_pairing_over_the_channel_confirms_on_the_phone(service, pairing, channel):
    await pair(service, pairing)

    assert "Gekoppelt" in channel.texts[-1]


async def test_a_wrong_code_is_rejected_and_creates_no_device(service, channel, repo):
    await service.handle(msg("/pair ZZZZZZZZ"))

    assert "abgelehnt" in channel.texts[-1]
    assert repo.list_devices() == []


# -- status ---------------------------------------------------------------------------------------


async def test_status_returns_only_allow_listed_scalars(service, pairing, channel):
    await pair(service, pairing)

    await service.handle(msg("/status", update_id=2))

    text = channel.texts[-1]
    assert "Modus: companion" in text
    assert "Privatsphäre: balanced" in text


async def test_status_never_contains_transcript_or_memory_fields(service, pairing, channel):
    """Allow-list assertion (ST-17-04): the status text is exactly what the provider returned."""
    await pair(service, pairing)

    await service.handle(msg("/status", update_id=2))

    for forbidden in ("transcript", "memory", "turn", "prompt", "token"):
        assert forbidden not in channel.texts[-1].lower()


# -- kill switch ----------------------------------------------------------------------------------


async def test_kill_from_the_phone_engages_as_a_user_origin(service, pairing, killswitch):
    await pair(service, pairing)

    await service.handle(msg("/kill", update_id=2))

    assert killswitch.engaged
    assert killswitch.origins == [REMOTE_KILL_ORIGIN]
    # Not a security-path kill: resuming locally needs no PIN (Security Model §6, OP-6 D).
    assert killswitch.security_path is False
    assert await killswitch.resume(pin_ok=False, by="dashboard") is True


async def test_the_phone_cannot_resume_what_it_killed(service, pairing, killswitch, channel, db):
    await pair(service, pairing)
    await service.handle(msg("/kill", update_id=2))

    decision = await service.handle(msg("/resume", update_id=3))

    assert not decision.allowed and decision.reason == "resume_not_remote"
    assert killswitch.engaged  # still engaged
    assert killswitch.resumes == []  # the service never even called resume
    assert "nur lokal" in channel.texts[-1]
    rows = db.fetch_all("SELECT command, decision FROM remote_audit WHERE command = ?", ("resume",))
    assert [(r["command"], r["decision"]) for r in rows] == [("resume", "deny")]


# -- privacy --------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["private", "offline"])
async def test_privacy_downgrade_is_applied(service, pairing, privacy, mode):
    await pair(service, pairing)

    await service.handle(msg(f"/privacy {mode}", update_id=2))

    assert privacy.mode is PrivacyMode(mode)


async def test_privacy_upgrade_is_denied_and_never_reaches_the_service(
    service, pairing, privacy, channel
):
    await pair(service, pairing)
    await service.handle(msg("/privacy offline", update_id=2))
    privacy.calls.clear()

    decision = await service.handle(msg("/privacy full", update_id=3))

    assert not decision.allowed and decision.reason == "privacy_upgrade_denied"
    assert privacy.calls == []  # the policy stopped it; `set_mode` was not even attempted
    assert privacy.mode is PrivacyMode.OFFLINE
    assert "private oder" in channel.texts[-1]


# -- revocation -----------------------------------------------------------------------------------


async def test_a_revoked_device_is_rejected_on_its_very_next_message(
    service, pairing, repo, killswitch
):
    await pair(service, pairing)
    device_id = repo.list_devices()[0].id
    await pairing.revoke(device_id, reason="dashboard")

    decision = await service.handle(msg("/kill", update_id=2))

    assert not decision.allowed and decision.reason == "not_paired"
    assert not killswitch.engaged


async def test_unpair_from_the_phone_revokes_itself(service, pairing, repo, channel):
    await pair(service, pairing)

    await service.handle(msg("/unpair", update_id=2))

    assert repo.list_devices()[0].revoked
    assert "entkoppelt" in channel.texts[-1]


# -- replay ---------------------------------------------------------------------------------------


async def test_a_replayed_message_is_rejected_and_does_not_act(service, pairing, killswitch, db):
    await pair(service, pairing)
    await service.handle(msg("/status", update_id=5))

    decision = await service.handle(msg("/kill", update_id=5))

    assert not decision.allowed and decision.reason == "replay"
    assert not killswitch.engaged
    rows = db.fetch_all("SELECT reason FROM remote_audit WHERE reason = ?", ("replay",))
    assert len(rows) == 1


# -- chat -----------------------------------------------------------------------------------------


async def test_chat_goes_through_the_orchestrator_and_answers(service, pairing, channel):
    await pair(service, pairing)

    await service.handle(msg("wie war dein Tag?", update_id=2))

    assert channel.texts[-1] == "Antwort auf: wie war dein Tag?"


async def test_a_failing_ai_is_reported_honestly(
    repo, pairing, killswitch, privacy, bus, audit, clock, channel
):
    async def broken_chat(text: str) -> str:
        raise RuntimeError("router down")

    service = RemoteService(
        repo=repo,
        pairing=pairing,
        policy=RemoteCommandPolicy(),
        killswitch=killswitch,
        privacy=privacy,
        send=channel,
        status=dict,
        chat=broken_chat,
        bus=bus,
        audit=audit,
        clock=clock,
    )
    await pair(service, pairing)

    await service.handle(msg("hallo", update_id=2))

    assert "nicht verfügbar" in channel.texts[-1]


# -- bus wiring -----------------------------------------------------------------------------------


async def test_the_service_reacts_to_remote_message_events(service, pairing, bus, killswitch):
    await pair(service, pairing)
    service.start()

    await bus.publish(Event(name=E.REMOTE_MESSAGE, payload=msg("/kill", update_id=2).model_dump()))

    assert killswitch.engaged
    service.stop()


async def test_a_malformed_event_payload_is_dropped_not_crashed(service, bus):
    service.start()

    await bus.publish(Event(name=E.REMOTE_MESSAGE, payload={"nonsense": True}))

    service.stop()


async def test_a_failing_send_never_breaks_the_handler(service, pairing, channel, killswitch):
    await pair(service, pairing)
    channel.fail = True

    decision = await service.handle(msg("/kill", update_id=2))

    assert decision.allowed and killswitch.engaged

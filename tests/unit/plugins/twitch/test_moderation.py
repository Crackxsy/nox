"""`ModerationGate.check` - each hard exclusion plus a benign message, and the configurable
blocklist on top (Spec v0.2 Stream Bot §3.4/FR-9.9)."""

from __future__ import annotations

from nox_plugin_twitch.moderation import ModerationGate


def test_benign_message_passes() -> None:
    gate = ModerationGate()
    ok, reason = gate.check("Thanks for the raid, welcome everyone!")
    assert ok is True
    assert reason == ""


def test_hate_speech_against_protected_group_is_blocked() -> None:
    gate = ModerationGate()
    ok, reason = gate.check("jews should die, all of them")
    assert ok is False
    assert reason == "hate_speech_protected_group"


def test_sexual_content_about_a_viewer_is_blocked() -> None:
    gate = ModerationGate()
    ok, reason = gate.check("hey @viewer1 send nudes please")
    assert ok is False
    assert reason == "sexual_content_about_viewer"


def test_doxxing_pattern_is_blocked() -> None:
    gate = ModerationGate()
    ok, reason = gate.check("here is their address: 12 Main street")
    assert ok is False
    assert reason == "doxxing"


def test_doxxing_phone_number_is_blocked() -> None:
    gate = ModerationGate()
    ok, reason = gate.check("call them at 555-123-4567 right now")
    assert ok is False
    assert reason == "doxxing"


def test_illness_or_death_joke_is_blocked() -> None:
    gate = ModerationGate()
    ok, reason = gate.check("lol just kys already")
    assert ok is False
    assert reason == "illness_or_death_joke"


def test_insult_aimed_at_a_viewer_name_is_blocked() -> None:
    gate = ModerationGate()
    ok, reason = gate.check("@viewer1 you are such an idiot")
    assert ok is False
    assert reason == "insult_aimed_at_viewer"


def test_configurable_blocklist_is_enforced() -> None:
    gate = ModerationGate(blocklist=["forbiddenword"])
    ok, reason = gate.check("this contains forbiddenword in it")
    assert ok is False
    assert reason == "blocklist:forbiddenword"


def test_blocklist_never_relaxes_the_hard_exclusions() -> None:
    """A hard exclusion is independent of any config (FR-9.9); an empty blocklist config still
    blocks hate speech."""
    gate = ModerationGate(blocklist=[])
    ok, _reason = gate.check("muslims should die")
    assert ok is False

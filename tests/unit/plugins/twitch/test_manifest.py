"""`plugins/twitch/manifest.yaml` validates on its own and against the real `stream` profile
(`config/profiles/stream.yaml`) - same approach as `tests/unit/plugins/obs/test_manifest.py`."""

from __future__ import annotations

from nox_plugin_twitch.settings import MOVED_KEYS

from nox.app import PROFILES_DIR, REPO_ROOT
from nox.plugins.manifest import check_egress, load_manifest
from nox.security.model import Risk
from nox.security.profiles import load_profile

TWITCH_PLUGIN_DIR = REPO_ROOT / "plugins" / "twitch"


def test_twitch_manifest_loads_and_declares_the_expected_tools() -> None:
    manifest = load_manifest(TWITCH_PLUGIN_DIR)
    assert manifest.id == "twitch"
    assert manifest.entry == "nox_plugin_twitch:create"
    assert manifest.matches_profile("stream") is True
    assert manifest.matches_profile("companion") is False
    tools = {p.tool: p.risk for p in manifest.permissions}
    assert tools == {
        "twitch.chat.send": Risk.LOW,
        "twitch.chat.status.read": Risk.READ,
    }
    # Not autonomous in the MVP (Spec v0.2 §3.4) and never present here at all.
    assert "twitch.moderation.timeout" not in tools
    assert "twitch.moderation.ban" not in tools


def test_twitch_manifest_events_match_events_py() -> None:
    manifest = load_manifest(TWITCH_PLUGIN_DIR)
    assert set(manifest.events.emits) == {
        "twitch.connected",
        "twitch.disconnected",
        "twitch.chat_message",
        "twitch.command_invoked",
        "stream.funken_awarded",
        "stream.minigame_started",
        "stream.minigame_ended",
    }
    assert manifest.events.listens == []


def test_twitch_manifest_secrets_are_declared() -> None:
    manifest = load_manifest(TWITCH_PLUGIN_DIR)
    assert manifest.secrets == ["nox/twitch/oauth_token", "nox/twitch/bot_username"]


def test_twitch_manifest_egress_is_authorized_by_the_real_stream_profile() -> None:
    manifest = load_manifest(TWITCH_PLUGIN_DIR)
    stream = load_profile(PROFILES_DIR / "stream.yaml")
    results = check_egress(manifest, stream)
    assert {(r.entry, r.allowed) for r in results} == {
        ("irc.chat.twitch.tv:6697", True),
        ("api.twitch.tv:443", True),
    }


def test_the_moved_knobs_are_gone_from_the_shipped_manifest() -> None:
    """#26: bot names, rate limits and backoff are `stream.twitch.*` now, not manifest keys.

    Leaving a copy here would silently win over the dashboard setting (the plugin honours a
    manifest key for one more release), which is exactly the problem #26 is about.
    """
    manifest = load_manifest(TWITCH_PLUGIN_DIR)
    assert sorted(set(manifest.config) & set(MOVED_KEYS)) == []
    # Still manifest-only: the endpoint, the moderation extras and the (unapproved) !rps numbers.
    assert manifest.config["channel"] == ""
    assert manifest.config["host"] == "irc.chat.twitch.tv"
    assert manifest.config["moderation_blocklist"] == []

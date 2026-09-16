"""Highlight candidate detector (ST-15-03, Spec v0.6 Clip Pipeline §4): scores `rl.event`,
`twitch.chat_mood_changed` and `!clip` (`twitch.command_invoked`) into `clip.requested`, with a
per-`trigger_kind` cooldown and no OBS call of any kind (the plugin declares no tools)."""

from __future__ import annotations

from nox_plugin_clips import create

from .conftest import FakeClient, make_api


async def _plugin(fake_client: FakeClient, **config_overrides):
    api = make_api(fake_client, **config_overrides)
    plugin = create(api)
    await plugin.start()
    return api, plugin


async def test_qualifying_rl_event_requests_a_clip(fake_client: FakeClient) -> None:
    api, plugin = await _plugin(fake_client)
    await fake_client.fire("rl.event", {"kind": "goal", "source": "hud", "confidence": 0.9})
    assert len(fake_client.events) == 1
    name, payload = fake_client.events[0]
    assert name == "clip.requested"
    assert payload["trigger_kind"] == "rl.goal"
    assert payload["source"] == "event"


async def test_non_highlight_rl_event_kind_is_ignored(fake_client: FakeClient) -> None:
    api, plugin = await _plugin(fake_client)
    await fake_client.fire("rl.event", {"kind": "boost_low", "source": "hud"})
    assert fake_client.events == []


async def test_cooldown_blocks_a_second_request_for_the_same_kind(fake_client: FakeClient) -> None:
    api, plugin = await _plugin(fake_client, cooldown_s=1000.0)
    await fake_client.fire("rl.event", {"kind": "goal", "source": "hud"})
    await fake_client.fire("rl.event", {"kind": "goal", "source": "hud"})
    assert len(fake_client.events) == 1


async def test_different_kinds_are_not_cross_throttled(fake_client: FakeClient) -> None:
    api, plugin = await _plugin(fake_client, cooldown_s=1000.0)
    await fake_client.fire("rl.event", {"kind": "goal", "source": "hud"})
    await fake_client.fire("rl.event", {"kind": "save", "source": "replay"})
    assert [e[1]["trigger_kind"] for e in fake_client.events] == ["rl.goal", "rl.save"]


async def test_chat_hype_category_requests_a_clip(fake_client: FakeClient) -> None:
    api, plugin = await _plugin(fake_client)
    await fake_client.fire("twitch.chat_mood_changed", {"category": "hype", "window": "short"})
    assert len(fake_client.events) == 1
    assert fake_client.events[0][1]["trigger_kind"] == "chat_hype"


async def test_non_hype_mood_is_ignored(fake_client: FakeClient) -> None:
    api, plugin = await _plugin(fake_client)
    await fake_client.fire("twitch.chat_mood_changed", {"category": "calm", "window": "short"})
    assert fake_client.events == []


async def test_clip_command_requests_a_manual_clip(fake_client: FakeClient) -> None:
    api, plugin = await _plugin(fake_client)
    await fake_client.fire(
        "twitch.command_invoked",
        {"chat_event_id": 42, "viewer_id": "v1", "command": "clip", "args": []},
    )
    assert len(fake_client.events) == 1
    name, payload = fake_client.events[0]
    assert name == "clip.requested"
    assert payload["trigger_kind"] == "user_marker"
    assert payload["source"] == "manual"
    assert payload["origin_event_id"] == "42"
    assert payload["tags"] == ["user_marker"]


async def test_other_commands_are_ignored(fake_client: FakeClient) -> None:
    api, plugin = await _plugin(fake_client)
    await fake_client.fire(
        "twitch.command_invoked", {"chat_event_id": 1, "command": "funken", "args": []}
    )
    assert fake_client.events == []


async def test_manual_clip_attaches_a_recent_rl_event_as_secondary_tag(
    fake_client: FakeClient,
) -> None:
    api, plugin = await _plugin(fake_client, manual_lookback_s=1000.0)
    await fake_client.fire("rl.event", {"kind": "goal", "source": "hud"})
    await fake_client.fire(
        "twitch.command_invoked", {"chat_event_id": 2, "command": "clip", "args": []}
    )
    manual_payload = fake_client.events[-1][1]
    assert manual_payload["tags"] == ["user_marker", "rl.goal"]


async def test_manual_clip_without_a_recent_rl_event_has_no_secondary_tag(
    fake_client: FakeClient,
) -> None:
    api, plugin = await _plugin(fake_client, manual_lookback_s=0.0)
    await fake_client.fire("rl.event", {"kind": "goal", "source": "hud"})
    await fake_client.fire(
        "twitch.command_invoked", {"chat_event_id": 3, "command": "clip", "args": []}
    )
    manual_payload = fake_client.events[-1][1]
    assert manual_payload["tags"] == ["user_marker"]


async def test_session_id_is_attached_from_stream_started(fake_client: FakeClient) -> None:
    api, plugin = await _plugin(fake_client)
    await fake_client.fire("stream.started", {"session_id": "abc123", "mode": "live"})
    await fake_client.fire("rl.event", {"kind": "goal", "source": "hud"})
    assert fake_client.events[0][1]["session_id"] == "abc123"


async def test_kill_switch_suppresses_further_requests(fake_client: FakeClient) -> None:
    api, plugin = await _plugin(fake_client)
    await fake_client.fire("security.kill_switch", {"by": "hotkey", "reason": "test"})
    await fake_client.fire("rl.event", {"kind": "goal", "source": "hud"})
    assert fake_client.events == []


async def test_panic_suppresses_further_requests(fake_client: FakeClient) -> None:
    api, plugin = await _plugin(fake_client)
    await fake_client.fire("security.panic", {"origin": "ui"})
    await fake_client.fire(
        "twitch.command_invoked", {"chat_event_id": 4, "command": "clip", "args": []}
    )
    assert fake_client.events == []

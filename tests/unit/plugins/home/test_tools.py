"""The `home.*` tool surface, exercised through the real `PluginApi` against a fake instance.

The important tests here are the negative ones. A lock, an alarm panel, a valve and a garage door
exist in the fake house; every one of them must be invisible to `home.list`, refused by
`home.state`, and impossible to actuate through any tool - and the refusal must be a structured
result the caller can show, never a traceback.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from .conftest import FakeClient, make_api, write_home_settings
from .fake_ha_server import FakeHomeAssistant, wait_until

pytestmark = pytest.mark.timeout(30)


async def _plugin(
    server: FakeHomeAssistant, client: FakeClient, user_config: Path, **settings: Any
) -> Any:
    """A started plugin whose configured host and port are the fake server's.

    The settings are written into the isolated User layer *before* the plugin is built, because
    that is the order the real worker sees: `resolve_settings` runs in `__init__`, and the URL it
    produces is what the manifest-scoped egress guard then authorizes.
    """
    from nox_plugin_home import create

    write_home_settings(
        user_config,
        host="127.0.0.1",
        port=server.port,
        min_backoff_s=0.05,
        max_backoff_s=0.2,
        **settings,
    )
    api = make_api(client, port=server.port)
    plugin = create(api)
    await plugin.start()
    await wait_until(lambda: plugin.client.authenticated)
    return api, plugin


async def test_registers_exactly_the_eleven_declared_tools(
    ha_server: FakeHomeAssistant, fake_client: FakeClient, home_config: Path
) -> None:
    api, plugin = await _plugin(ha_server, fake_client, home_config)
    try:
        assert api.tools.names() == sorted(
            [
                "home.automation.trigger",
                "home.climate",
                "home.cover",
                "home.light",
                "home.list",
                "home.media",
                "home.scene",
                "home.script",
                "home.state",
                "home.status.read",
                "home.switch",
            ]
        )
        assert api.tools.get("home.script").risk.value == "high"
        assert api.tools.get("home.light").risk.value == "medium"
        assert api.tools.get("home.list").risk.value == "read"
    finally:
        await plugin.stop()


async def test_list_hides_every_entity_that_secures_the_building(
    ha_server: FakeHomeAssistant, fake_client: FakeClient, home_config: Path
) -> None:
    api, plugin = await _plugin(ha_server, fake_client, home_config)
    try:
        result = await api.tools.call("home.list", {})
        ids = {row["entity_id"] for row in result["entities"]}
        assert "light.wz_decke" in ids
        for hidden in (
            "lock.haustuer",
            "alarm_control_panel.haus",
            "valve.wasser",
            "cover.garage",
            "person.someone",
        ):
            assert hidden not in ids
        assert set(result["areas"]) == {"Wohnzimmer", "Küche", "Bad"}
        assert result["areas_available"] is True
    finally:
        await plugin.stop()


async def test_list_reports_the_room_of_every_entity(
    ha_server: FakeHomeAssistant, fake_client: FakeClient, home_config: Path
) -> None:
    api, plugin = await _plugin(ha_server, fake_client, home_config)
    try:
        result = await api.tools.call("home.list", {"area": "Wohnzimmer", "domain": "light"})
        # `light.wz_steh` has no area of its own; it inherits its device's room.
        assert {row["entity_id"] for row in result["entities"]} == {
            "light.wz_decke",
            "light.wz_steh",
        }
    finally:
        await plugin.stop()


async def test_list_keeps_only_allow_listed_attributes(
    ha_server: FakeHomeAssistant, fake_client: FakeClient, home_config: Path
) -> None:
    """`friendly_name` becomes the name; nothing else outside the allow-list survives."""
    ha_server.set_response(
        "get_states",
        lambda _d: [
            {
                "entity_id": "media_player.wz",
                "state": "playing",
                "attributes": {
                    "friendly_name": "Fernseher",
                    "volume_level": 0.4,
                    "media_title": "something private",
                    "entity_picture": "/api/media_player_proxy/x",
                },
            }
        ],
    )
    api, plugin = await _plugin(ha_server, fake_client, home_config)
    try:
        result = await api.tools.call("home.list", {})
        attributes = result["entities"][0]["attributes"]
        assert attributes == {"volume_level": 0.4}
        assert "media_title" not in attributes
    finally:
        await plugin.stop()


async def test_state_refuses_a_lock_with_a_reason(
    ha_server: FakeHomeAssistant, fake_client: FakeClient, home_config: Path
) -> None:
    api, plugin = await _plugin(ha_server, fake_client, home_config)
    try:
        result = await api.tools.call("home.state", {"entity_id": "lock.haustuer"})
        assert result["ok"] is False
        assert result["refused"] is True
        assert "lock" in result["reason"]
        assert result["entity"] is None
    finally:
        await plugin.stop()


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("home.switch", {"entity_ids": ["lock.haustuer"], "on": False}),
        ("home.switch", {"entity_ids": ["alarm_control_panel.haus"], "on": True}),
        ("home.cover", {"entity_ids": ["cover.garage"], "action": "open"}),
        ("home.switch", {"entity_ids": ["valve.wasser"], "on": True}),
    ],
)
async def test_no_tool_can_actuate_a_forbidden_entity(
    ha_server: FakeHomeAssistant,
    fake_client: FakeClient,
    home_config: Path,
    tool: str,
    arguments: dict[str, Any],
) -> None:
    api, plugin = await _plugin(ha_server, fake_client, home_config)
    try:
        result = await api.tools.call(tool, arguments)
        assert result["ok"] is False
        assert result["refused"] is True
        assert ha_server.service_calls == []
    finally:
        await plugin.stop()


async def test_light_off_calls_the_right_service(
    ha_server: FakeHomeAssistant, fake_client: FakeClient, home_config: Path
) -> None:
    api, plugin = await _plugin(ha_server, fake_client, home_config)
    try:
        result = await api.tools.call(
            "home.light", {"entity_ids": ["light.wz_decke", "light.wz_steh"], "on": False}
        )
        assert result["ok"] is True
        call = ha_server.service_calls[-1]
        assert call["domain"] == "light"
        assert call["service"] == "turn_off"
        assert call["target"]["entity_id"] == ["light.wz_decke", "light.wz_steh"]
    finally:
        await plugin.stop()


async def test_light_brightness_is_sent_as_a_percentage(
    ha_server: FakeHomeAssistant, fake_client: FakeClient, home_config: Path
) -> None:
    api, plugin = await _plugin(ha_server, fake_client, home_config)
    try:
        await api.tools.call("home.light", {"entity_ids": ["light.wz_decke"], "brightness_pct": 30})
        call = ha_server.service_calls[-1]
        assert call["service"] == "turn_on"
        assert call["service_data"] == {"brightness_pct": 30}
    finally:
        await plugin.stop()


async def test_media_volume_is_converted_to_home_assistants_scale(
    ha_server: FakeHomeAssistant, fake_client: FakeClient, home_config: Path
) -> None:
    api, plugin = await _plugin(ha_server, fake_client, home_config)
    try:
        await api.tools.call(
            "home.media",
            {"entity_ids": ["media_player.wz"], "action": "volume_set", "volume_pct": 25},
        )
        call = ha_server.service_calls[-1]
        assert call["service"] == "volume_set"
        assert call["service_data"] == {"volume_level": 0.25}
    finally:
        await plugin.stop()


async def test_climate_sets_the_target_temperature(
    ha_server: FakeHomeAssistant, fake_client: FakeClient, home_config: Path
) -> None:
    api, plugin = await _plugin(ha_server, fake_client, home_config)
    try:
        await api.tools.call("home.climate", {"entity_ids": ["climate.bad"], "temperature_c": 21})
        call = ha_server.service_calls[-1]
        assert call["domain"] == "climate"
        assert call["service"] == "set_temperature"
        assert call["service_data"] == {"temperature": 21.0}
    finally:
        await plugin.stop()


async def test_automation_trigger_never_skips_the_automations_conditions(
    ha_server: FakeHomeAssistant, fake_client: FakeClient, home_config: Path
) -> None:
    api, plugin = await _plugin(ha_server, fake_client, home_config)
    try:
        await api.tools.call("home.automation.trigger", {"entity_id": "automation.morgens"})
        call = ha_server.service_calls[-1]
        assert call["service_data"] == {"skip_condition": False}
    finally:
        await plugin.stop()


async def test_a_wrong_domain_for_a_tool_is_a_clear_message_not_a_crash(
    ha_server: FakeHomeAssistant, fake_client: FakeClient, home_config: Path
) -> None:
    api, plugin = await _plugin(ha_server, fake_client, home_config)
    try:
        result = await api.tools.call("home.light", {"entity_ids": ["switch.kaffee"], "on": True})
        assert result["ok"] is False
        assert "home.switch" in result["reason"]
        assert ha_server.service_calls == []
    finally:
        await plugin.stop()


async def test_areas_allowed_restricts_which_rooms_can_be_acted_on(
    ha_server: FakeHomeAssistant, fake_client: FakeClient, home_config: Path
) -> None:
    api, plugin = await _plugin(ha_server, fake_client, home_config, areas_allowed=["Wohnzimmer"])
    try:
        assert plugin.settings.areas_allowed == ["Wohnzimmer"]
        allowed = await api.tools.call("home.light", {"entity_ids": ["light.wz_decke"], "on": True})
        assert allowed["ok"] is True
        blocked = await api.tools.call("home.light", {"entity_ids": ["light.kueche"], "on": True})
        assert blocked["ok"] is False
        assert "home.areas_allowed" in blocked["reason"]
    finally:
        await plugin.stop()


async def test_an_unknown_entity_is_refused_before_any_service_call(
    ha_server: FakeHomeAssistant, fake_client: FakeClient, home_config: Path
) -> None:
    api, plugin = await _plugin(ha_server, fake_client, home_config)
    try:
        result = await api.tools.call("home.light", {"entity_ids": ["light.nope"], "on": True})
        assert result["ok"] is False
        assert "not an entity Nox can see" in result["reason"]
        assert ha_server.service_calls == []
    finally:
        await plugin.stop()


async def test_state_changes_are_forwarded_as_events(
    ha_server: FakeHomeAssistant, fake_client: FakeClient, home_config: Path
) -> None:
    api, plugin = await _plugin(ha_server, fake_client, home_config)
    try:
        await api.tools.call("home.list", {})  # prime the inventory
        await ha_server.broadcast_state_changed("light.wz_decke", "off", {"friendly_name": "x"})
        await wait_until(lambda: "home.state_changed" in fake_client.names())
        payload = dict(fake_client.events[-1][1])
        assert payload["entity_id"] == "light.wz_decke"
        assert payload["state"] == "off"
        assert payload["area"] == "Wohnzimmer"
    finally:
        await plugin.stop()


async def test_a_forbidden_entitys_state_change_is_never_forwarded(
    ha_server: FakeHomeAssistant, fake_client: FakeClient, home_config: Path
) -> None:
    api, plugin = await _plugin(ha_server, fake_client, home_config)
    try:
        await api.tools.call("home.list", {})
        await ha_server.broadcast_state_changed("lock.haustuer", "unlocked")
        await ha_server.broadcast_state_changed("light.wz_steh", "on")
        await wait_until(lambda: "home.state_changed" in fake_client.names())
        forwarded = [
            p["entity_id"] for name, p in fake_client.events if name == "home.state_changed"
        ]
        assert "lock.haustuer" not in forwarded
        assert "light.wz_steh" in forwarded
    finally:
        await plugin.stop()

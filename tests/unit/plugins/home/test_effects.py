"""The home boundary by effect, against a fake Home Assistant.

A scene is judged by the entities it sets, a script or automation by its definition, a switch or
cover by what it probably is. These are the negative cases: a scene that unlocks the front door, a
script that calls `lock.unlock`, a garage relay disguised as a switch - each must be refused or
confirmation-gated with the entity named, and a forbidden one must not reach Home Assistant even
when the tool is called directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from nox_plugin_home.effects import CONFIG_TTL_S, DefinitionCache

from .conftest import FakeClient, make_api, write_home_settings
from .fake_ha_server import FakeHomeAssistant, effect_states, wait_until

pytestmark = pytest.mark.timeout(30)


async def _plugin(server: FakeHomeAssistant, client: FakeClient, user_config: Path) -> Any:
    from nox_plugin_home import create

    server.set_response("get_states", lambda _d: effect_states())
    write_home_settings(
        user_config, host="127.0.0.1", port=server.port, min_backoff_s=0.05, max_backoff_s=0.2
    )
    api = make_api(client, port=server.port)
    plugin = create(api)
    await plugin.start()
    await wait_until(lambda: plugin.client.authenticated)
    return api, plugin


async def _effect(api: Any, tool: str, payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = await api.tools.call("home.effect", {"tool": tool, "input": payload})
    return result


@pytest.fixture
async def started(ha_server: FakeHomeAssistant, fake_client: FakeClient, home_config: Path) -> Any:
    api, plugin = await _plugin(ha_server, fake_client, home_config)
    try:
        yield api
    finally:
        await plugin.stop()


async def test_a_scene_of_lights_and_media_is_allowed_and_lists_its_members(started: Any) -> None:
    verdict = await _effect(started, "home.scene", {"entity_id": "scene.kino"})
    assert verdict["decision"] == "allow"
    assert verdict["targets"] == ["scene.kino"]


@pytest.mark.parametrize(
    ("scene", "culprit"),
    [("scene.abschied", "lock.haustuer"), ("scene.garage_auf", "cover.garage")],
)
async def test_a_scene_that_touches_a_forbidden_entity_is_refused(
    started: Any, scene: str, culprit: str
) -> None:
    verdict = await _effect(started, "home.scene", {"entity_id": scene})
    assert verdict["decision"] == "deny"
    assert verdict["targets"] == [scene, culprit]
    assert "never exposed" in verdict["reason"] or "secures the building" in verdict["reason"]


async def test_a_scene_that_does_not_list_its_members_needs_a_confirmation(started: Any) -> None:
    verdict = await _effect(started, "home.scene", {"entity_id": "scene.hue_relax"})
    assert verdict["decision"] == "confirm"
    assert verdict["targets"] == ["scene.hue_relax"]


async def test_a_scene_that_switches_a_gate_relay_needs_a_confirmation_naming_it(
    started: Any,
) -> None:
    verdict = await _effect(started, "home.scene", {"entity_id": "scene.hof"})
    assert verdict["decision"] == "confirm"
    assert verdict["targets"] == ["scene.hof", "switch.hoftor"]


@pytest.mark.parametrize("entity_id", ["switch.hoftor", "switch.relais_2"])
async def test_a_switch_that_looks_like_an_entrance_relay_needs_a_confirmation(
    started: Any, entity_id: str
) -> None:
    verdict = await _effect(started, "home.switch", {"entity_ids": [entity_id], "on": True})
    assert verdict["decision"] == "confirm"
    assert verdict["targets"] == [entity_id]


async def test_an_ordinary_switch_stays_pre_approved(started: Any) -> None:
    verdict = await _effect(started, "home.switch", {"entity_ids": ["switch.kaffee"], "on": True})
    assert verdict["decision"] == "allow"


async def test_a_cover_without_a_window_covering_class_needs_a_confirmation(
    started: Any,
) -> None:
    unclear = await _effect(
        started, "home.cover", {"entity_ids": ["cover.unklar"], "action": "open"}
    )
    blind = await _effect(
        started, "home.cover", {"entity_ids": ["cover.markise", "cover.wz_rollo"], "action": "open"}
    )
    garage = await _effect(
        started, "home.cover", {"entity_ids": ["cover.garage"], "action": "open"}
    )
    assert unclear["decision"] == "confirm"
    assert blind["decision"] == "allow"
    assert garage["decision"] == "deny"


async def test_a_script_that_calls_a_lock_service_is_refused(started: Any) -> None:
    verdict = await _effect(started, "home.script", {"entity_id": "script.aufsperren"})
    assert verdict["decision"] == "deny"
    assert "lock.*" in verdict["targets"] or "lock.haustuer" in verdict["targets"]


async def test_an_automation_with_a_lock_device_action_is_refused(started: Any) -> None:
    verdict = await _effect(started, "home.automation.trigger", {"entity_id": "automation.abends"})
    assert verdict["decision"] == "deny"


async def test_a_script_that_targets_a_whole_area_needs_a_confirmation(started: Any) -> None:
    verdict = await _effect(started, "home.script", {"entity_id": "script.alles_aus"})
    assert verdict["decision"] == "confirm"
    assert "area" in verdict["targets"]


async def test_a_plain_script_is_allowed_by_the_check_and_left_to_the_profile(
    started: Any,
) -> None:
    verdict = await _effect(started, "home.script", {"entity_id": "script.gute_nacht"})
    assert verdict["decision"] == "allow"


async def test_a_definition_the_token_may_not_read_needs_a_confirmation(
    ha_server: FakeHomeAssistant, started: Any
) -> None:
    ha_server.definitions.pop("script.gute_nacht")
    verdict = await _effect(started, "home.script", {"entity_id": "script.gute_nacht"})
    assert verdict["decision"] == "confirm"


async def test_the_forbidden_scene_never_reaches_home_assistant_even_when_called_directly(
    ha_server: FakeHomeAssistant, started: Any
) -> None:
    result = await started.tools.call("home.scene", {"entity_id": "scene.abschied"})
    script = await started.tools.call("home.script", {"entity_id": "script.aufsperren"})
    assert result["ok"] is False and result["refused"] is True
    assert script["ok"] is False and script["refused"] is True
    assert ha_server.service_calls == []


async def test_a_safe_scene_is_still_activated(ha_server: FakeHomeAssistant, started: Any) -> None:
    result = await started.tools.call("home.scene", {"entity_id": "scene.kino"})
    assert result["ok"] is True
    assert ha_server.service_calls[-1]["domain"] == "scene"


async def test_the_effect_check_refuses_when_not_connected(
    fake_client: FakeClient, home_config: Path
) -> None:
    from nox_plugin_home import create

    write_home_settings(home_config, host="127.0.0.1", port=1)
    api = make_api(fake_client, port=1)
    create(api)
    verdict = await _effect(api, "home.scene", {"entity_id": "scene.kino"})
    assert verdict["decision"] == "deny"


async def test_unknown_tools_and_bad_input_are_refused(started: Any) -> None:
    unknown = await _effect(started, "home.light", {"entity_ids": ["light.kueche"]})
    bad = await _effect(started, "home.scene", {"entity_id": "not an id"})
    assert unknown["decision"] == "deny"
    assert bad["decision"] == "deny"


async def test_definitions_are_cached_for_the_ttl() -> None:
    reads: list[str] = []
    now = [0.0]

    async def command(_name: str, payload: Any) -> Any:
        reads.append(payload["entity_id"])
        return {"config": {"sequence": []}}

    cache = DefinitionCache(command, clock=lambda: now[0])
    await cache.get("script.a")
    await cache.get("script.a")
    now[0] = CONFIG_TTL_S + 1
    await cache.get("script.a")
    assert reads == ["script.a", "script.a"]

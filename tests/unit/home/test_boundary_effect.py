"""The boundary by effect: what a scene, script, automation, switch or cover really changes.

Pure functions, no Home Assistant: `nox_plugin_home.effects` feeds them from the real instance,
`tests/unit/plugins/home/test_effects.py` covers that half.
"""

from __future__ import annotations

from typing import Any

import pytest

from nox.home.boundary import (
    Effect,
    config_effect,
    config_references,
    entity_effect,
    looks_like_entrance,
    members_effect,
)


@pytest.mark.parametrize(
    ("entity_id", "name"),
    [
        ("switch.garage", ""),
        ("switch.relay_1", "Garagentor"),
        ("switch.relay_2", "Hoftor"),
        ("switch.tor", ""),
        ("switch.front_door_opener", ""),
        ("switch.x", "Türöffner"),
        ("switch.x", "Haustuer"),
        ("switch.einfahrt", ""),
        ("switch.gate_relay", ""),
    ],
)
def test_entrance_relays_are_recognised_by_name(entity_id: str, name: str) -> None:
    assert looks_like_entrance(entity_id, name) is True


@pytest.mark.parametrize(
    ("entity_id", "name"),
    [
        ("switch.kaffee", "Kaffeemaschine"),
        ("switch.pool_motor", "Poolmotor"),
        ("switch.monitor", "Monitor"),
        ("switch.indoor_lights", "Indoor lights"),
        ("switch.ventilator", "Ventilator"),
    ],
)
def test_ordinary_switches_are_not_mistaken_for_entrances(entity_id: str, name: str) -> None:
    assert looks_like_entrance(entity_id, name) is False


def test_a_switch_whose_device_class_says_garage_is_an_entrance() -> None:
    assert looks_like_entrance("switch.relay_3", "", {"device_class": "garage"}) is True


def test_entity_effect_grades_forbidden_confirm_and_safe() -> None:
    assert entity_effect("lock.front").effect is Effect.FORBIDDEN
    assert entity_effect("cover.x", {"device_class": "garage"}).effect is Effect.FORBIDDEN
    assert entity_effect("cover.x", {}).effect is Effect.CONFIRM
    assert entity_effect("cover.x", {"device_class": "window"}).effect is Effect.CONFIRM
    assert entity_effect("cover.x", {"device_class": "blind"}).effect is Effect.SAFE
    assert entity_effect("switch.garage_relay").effect is Effect.CONFIRM
    assert entity_effect("light.kitchen").effect is Effect.SAFE


def test_a_scene_is_as_strict_as_its_strictest_member() -> None:
    attributes = {"cover.garage": {"device_class": "garage"}}
    assert members_effect(["light.a", "media_player.b"], {}).effect is Effect.SAFE
    confirm = members_effect(["light.a", "switch.hoftor", "cover.plain"], {})
    assert confirm.effect is Effect.CONFIRM
    assert confirm.entities == ("switch.hoftor", "cover.plain")
    forbidden = members_effect(["switch.hoftor", "cover.garage"], attributes)
    assert forbidden.effect is Effect.FORBIDDEN and forbidden.entities == ("cover.garage",)


SCRIPT_FORMS: list[tuple[str, Any]] = [
    ("legacy service key", {"sequence": [{"service": "lock.unlock", "entity_id": "lock.a"}]}),
    ("action key", {"sequence": [{"action": "lock.unlock", "target": {"entity_id": ["lock.a"]}}]}),
    ("device action", {"action": [{"device_id": "d1", "domain": "lock", "type": "unlock"}]}),
    ("alarm", {"sequence": [{"action": "alarm_control_panel.alarm_disarm", "target": {}}]}),
    ("valve in a choose", {"sequence": [{"choose": [{"sequence": [{"action": "valve.open"}]}]}]}),
    ("garage cover", {"sequence": [{"action": "cover.open_cover", "entity_id": "cover.garage"}]}),
]


@pytest.mark.parametrize(("label", "config"), SCRIPT_FORMS, ids=[f for f, _ in SCRIPT_FORMS])
def test_every_way_a_definition_can_reach_a_lock_is_forbidden(label: str, config: Any) -> None:
    references = config_references(config)
    effect = config_effect(references, {"cover.garage": {"device_class": "garage"}})
    assert effect.effect is Effect.FORBIDDEN, label


@pytest.mark.parametrize(
    "config",
    [
        {"sequence": [{"action": "light.turn_off", "target": {"area_id": "living"}}]},
        {"sequence": [{"action": "light.turn_off", "target": {"entity_id": "{{ x }}"}}]},
        {"sequence": [{"action": "{{ 'lock' }}.unlock"}]},
        {"sequence": [{"action": "script.turn_on", "target": {"entity_id": "script.other"}}]},
        {"sequence": [{"device_id": "d1", "type": "turn_on"}]},
    ],
)
def test_what_cannot_be_pinned_down_needs_a_confirmation(config: Any) -> None:
    assert config_effect(config_references(config), {}).effect is Effect.CONFIRM


def test_a_plain_definition_is_safe_by_effect() -> None:
    config = {
        "trigger": [{"platform": "state", "entity_id": "binary_sensor.motion"}],
        "action": [{"action": "light.turn_on", "target": {"entity_id": "light.hall"}}],
    }
    assert config_effect(config_references(config), {}).effect is Effect.SAFE

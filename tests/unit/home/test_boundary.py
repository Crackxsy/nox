"""The hard boundary: what Nox is never allowed to see or touch in a home.

Every assertion here is a negative one, because that is what the rule is. A language model must not
be able to unlock a door, and "the permission engine would ask first" is not the same guarantee -
these entities have no tool, no listing entry and no code path at all.
"""

from __future__ import annotations

import pytest

from nox.home.boundary import (
    CONTROLLABLE_DOMAINS,
    DOMAIN_TOOLS,
    FORBIDDEN_COVER_DEVICE_CLASSES,
    FORBIDDEN_DOMAINS,
    ForbiddenEntityError,
    domain_of,
    forbidden_reason,
    is_exposed,
    require_allowed,
)


@pytest.mark.parametrize(
    "entity_id",
    ["lock.front_door", "alarm_control_panel.house", "valve.main_water"],
)
def test_a_building_securing_domain_is_never_exposed(entity_id: str) -> None:
    assert forbidden_reason(entity_id) is not None
    assert is_exposed(entity_id) is False
    with pytest.raises(ForbiddenEntityError):
        require_allowed(entity_id)


@pytest.mark.parametrize("device_class", sorted(FORBIDDEN_COVER_DEVICE_CLASSES))
def test_a_cover_that_is_an_entrance_is_treated_like_a_lock(device_class: str) -> None:
    """`cover` carries blinds and garage doors; only the blinds are Nox's business."""
    attributes = {"device_class": device_class}
    assert is_exposed("cover.front", attributes) is False
    with pytest.raises(ForbiddenEntityError):
        require_allowed("cover.front", expected_domain="cover", attributes=attributes)


def test_a_blind_is_a_cover_nox_may_control() -> None:
    attributes = {"device_class": "blind"}
    assert is_exposed("cover.living_room", attributes) is True
    assert require_allowed("cover.living_room", expected_domain="cover", attributes=attributes) == (
        "cover"
    )


def test_a_cover_without_a_device_class_is_treated_as_a_blind() -> None:
    """Home Assistant leaves `device_class` unset for most blinds; refusing them all would make
    the feature useless, and an entrance almost always declares itself."""
    assert is_exposed("cover.bedroom", {}) is True


def test_people_are_not_devices() -> None:
    assert is_exposed("person.someone") is False
    assert is_exposed("device_tracker.a_phone") is False


def test_every_controllable_domain_has_exactly_one_tool() -> None:
    assert set(DOMAIN_TOOLS) == CONTROLLABLE_DOMAINS
    assert len(set(DOMAIN_TOOLS.values())) == len(DOMAIN_TOOLS)
    for domain in FORBIDDEN_DOMAINS:
        assert domain not in DOMAIN_TOOLS


def test_a_domain_nobody_thought_about_is_invisible_rather_than_reachable() -> None:
    """The allow-list is what keeps a future Home Assistant domain from being silently usable."""
    assert is_exposed("water_heater.boiler") is False
    assert is_exposed("vacuum.robot") is False
    with pytest.raises(ValueError, match="cannot control"):
        require_allowed("vacuum.robot")


def test_the_wrong_tool_for_a_domain_names_the_right_one() -> None:
    with pytest.raises(ValueError, match="home.switch"):
        require_allowed("switch.kettle", expected_domain="light")


def test_a_malformed_entity_id_is_a_validation_error() -> None:
    assert domain_of("nonsense") == ""
    with pytest.raises(ValueError, match="entity id"):
        require_allowed("nonsense")

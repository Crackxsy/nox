"""The pet variants the configuration offers, held against the art that actually ships.

`pet.variant` is a closed list so the dashboard can offer it as a choice instead of a text box
nobody knows what to type into. A closed list in Python next to a folder of art in the UI is two
sources of truth, and the failure modes are both silent: a creature drawn, rigged and shipped that
no setting can select, or a setting that selects a creature whose art was never added. Either one
is caught here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nox.core.config.assistant import PET_VARIANTS, PetConfig

VARIANTS_DIR = Path(__file__).resolve().parents[3] / "ui" / "pet" / "public" / "variants"
#: Art that exists for development and must not be offered to a user.
NOT_FOR_USERS = {"placeholder"}


def _shipped() -> set[str]:
    return {
        path.name
        for path in VARIANTS_DIR.iterdir()
        if (path / "rig.json").exists() and path.name not in NOT_FOR_USERS
    }


def test_every_rigged_creature_can_be_selected() -> None:
    offered = {
        value.removeprefix("sprite:") for value in PET_VARIANTS if value.startswith("sprite:")
    }
    assert _shipped() - offered == set(), "rigged art that no setting can select"


def test_every_offered_creature_has_its_art() -> None:
    for value in PET_VARIANTS:
        if not value.startswith("sprite:"):
            continue
        folder = VARIANTS_DIR / value.removeprefix("sprite:")
        assert (folder / "rig.json").exists(), f"{value} is offered but has no rig"
        assert (folder / "sprites.json").exists(), f"{value} has no still-frame fallback"


def test_the_default_is_offered() -> None:
    assert PetConfig().variant in PET_VARIANTS


@pytest.mark.parametrize("value", ["sprite:does-not-exist", "Fox", ""])
def test_an_unknown_variant_falls_back_instead_of_refusing_to_start(value: str) -> None:
    # A configuration written before the list was closed must still load.
    assert PetConfig(variant=value).variant == "neutral"

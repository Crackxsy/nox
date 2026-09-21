"""The deterministic intent layer: the sentences it must understand, and the ones it must not.

The second half is the load-bearing one. A matcher that guesses is worse than one that gives up,
because a guess switches off the wrong room; every "returns None" test below is a case where the
honest answer is to hand the sentence to the model.

The last test measures the matching latency, which is the whole claim of this layer: resolving
"mach das licht im wohnzimmer aus" must not cost an AI round trip.
"""

from __future__ import annotations

import time

import pytest

from nox.home.intent import (
    IntentMatch,
    IntentRefusal,
    IntentSnapshot,
    KnownEntity,
    normalize,
    resolve_intent,
)

#: A small house with two rooms, one of every kind of device, and German names with umlauts.
HOUSE = IntentSnapshot(
    entities=(
        KnownEntity("light.wz_decke", "Deckenlampe", "Wohnzimmer", "on"),
        KnownEntity("light.wz_steh", "Stehlampe", "Wohnzimmer", "off"),
        KnownEntity("light.kueche", "Küchenlicht", "Küche", "on"),
        KnownEntity("switch.kaffee", "Kaffeemaschine", "Küche", "off"),
        KnownEntity("media_player.wz", "Fernseher", "Wohnzimmer", "playing"),
        KnownEntity("climate.bad", "Heizung Bad", "Bad", "heat"),
        KnownEntity("cover.wz_rollo", "Rollladen", "Wohnzimmer", "open", {"device_class": "blind"}),
        KnownEntity("scene.kino", "Kinoabend", "Wohnzimmer", "unknown"),
        KnownEntity("script.gute_nacht", "Gute Nacht Routine", "", "off"),
        KnownEntity("sensor.wz_temp", "Temperatur", "Wohnzimmer", "21.5"),
    ),
    areas=("Wohnzimmer", "Küche", "Bad"),
)


def _match(text: str) -> IntentMatch:
    result = resolve_intent(text, HOUSE)
    assert isinstance(result, IntentMatch), f"{text!r} did not match: {result!r}"
    return result


# ---- what it must understand ---------------------------------------------------------------------


def test_the_sentence_the_feature_exists_for() -> None:
    match = _match("mach das licht im wohnzimmer aus")
    assert match.tool == "home.light"
    assert match.arguments == {"entity_ids": ["light.wz_decke", "light.wz_steh"], "on": False}
    assert match.summary == "Licht Wohnzimmer: aus (2 Geräte)"


def test_an_area_plus_a_device_word_selects_every_device_of_that_kind_in_that_room() -> None:
    match = _match("schalte das licht in der küche an")
    assert match.arguments == {"entity_ids": ["light.kueche"], "on": True}


def test_a_room_written_without_its_umlaut_still_matches() -> None:
    """Speech-to-text and keyboards both produce "kueche"; Home Assistant has "Küche"."""
    assert _match("mach das licht in der kueche aus").entity_ids == ("light.kueche",)


def test_a_typo_in_the_room_name_still_matches() -> None:
    assert _match("mach das licht im wohnzimer aus").tool == "home.light"


def test_an_entity_named_directly_needs_no_word_for_its_kind() -> None:
    match = _match("schalte die stehlampe an")
    assert match.arguments == {"entity_ids": ["light.wz_steh"], "on": True}


def test_a_socket_named_directly_resolves_to_the_switch_tool() -> None:
    match = _match("mach die kaffeemaschine an")
    assert match.tool == "home.switch"
    assert match.arguments == {"entity_ids": ["switch.kaffee"], "on": True}


def test_brightness_in_percent() -> None:
    match = _match("dimm das licht im wohnzimmer auf 30 prozent")
    assert match.arguments["brightness_pct"] == 30
    assert match.arguments["on"] is True


def test_relative_brightness() -> None:
    assert _match("mach das licht im wohnzimmer heller").arguments["brightness_step_pct"] == 20
    assert _match("mach das licht im wohnzimmer dunkler").arguments["brightness_step_pct"] == -20


def test_colour_temperature() -> None:
    assert _match("mach das licht im wohnzimmer wärmer").arguments["color_temp_kelvin"] == 2700


def test_media_pause_and_volume() -> None:
    assert _match("pausier den fernseher").arguments["action"] == "pause"
    assert _match("mach den fernseher lauter").arguments["action"] == "volume_up"


def test_a_target_temperature() -> None:
    match = _match("stell die heizung im bad auf 21 grad")
    assert match.tool == "home.climate"
    assert match.arguments == {"entity_ids": ["climate.bad"], "temperature_c": 21.0}


def test_blinds_open_and_close() -> None:
    assert _match("mach den rollladen im wohnzimmer zu").arguments["action"] == "close"
    assert _match("mach den rollladen im wohnzimmer hoch").arguments["action"] == "open"


def test_a_scene_by_its_name() -> None:
    match = _match("szene kinoabend")
    assert match.tool == "home.scene"
    assert match.arguments == {"entity_id": "scene.kino"}


def test_a_script_is_matched_but_stays_a_high_risk_tool() -> None:
    match = _match("starte die gute nacht routine")
    assert match.tool == "home.script"
    assert match.arguments == {"entity_id": "script.gute_nacht"}


def test_everything_of_one_kind_at_once() -> None:
    match = _match("alle lichter aus")
    assert set(match.entity_ids) == {"light.wz_decke", "light.wz_steh", "light.kueche"}


def test_english_works_against_german_room_names() -> None:
    match = _match("turn off the lights in the wohnzimmer")
    assert match.arguments["on"] is False


# ---- what it must refuse ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "schließ die haustür auf",
        "mach die garage auf",
        "schalte die alarmanlage scharf",
        "unlock the front door",
        "open the garage",
        "mach das ventil zu",
    ],
)
def test_anything_about_locks_alarms_garages_or_valves_is_refused(text: str) -> None:
    result = resolve_intent(text, HOUSE)
    assert isinstance(result, IntentRefusal)
    assert result.matched_word
    assert "hard boundary" in result.reason


def test_the_refusal_does_not_depend_on_owning_such_a_device() -> None:
    """The house in these tests has no lock at all; the answer must still be the same."""
    empty = IntentSnapshot()
    assert isinstance(resolve_intent("mach die haustür auf", empty), IntentRefusal)


# ---- what it must hand to the model -----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "mach das wohnzimmer aus",  # a room, but no kind of device: ambiguous
        "mach das licht im arbeitszimmer aus",  # a room that does not exist
        "mach das licht im bad aus",  # a room without a single light
        "wie warm ist es im wohnzimmer",  # a question, not a command
        "erzähl mir einen witz",
        "",
        "   ",
    ],
)
def test_a_sentence_it_does_not_understand_is_handed_to_the_model(text: str) -> None:
    assert resolve_intent(text, HOUSE) is None


def test_a_sensor_is_readable_but_never_a_target() -> None:
    """`sensor.wz_temp` is in the snapshot; no verb may resolve onto it."""
    for text in ("mach die temperatur an", "schalte die temperatur aus"):
        result = resolve_intent(text, HOUSE)
        assert result is None or "sensor.wz_temp" not in getattr(result, "entity_ids", ())


def test_normalize_folds_case_and_diacritics() -> None:
    assert normalize("Mach das Licht im BÜRO aus") == (
        "mach",
        "das",
        "licht",
        "im",
        "buro",
        "aus",
    )


# ---- the claim this layer makes ---------------------------------------------------------------

#: A house far bigger than the one above, to measure the cost where it actually matters.
LARGE_HOUSE = IntentSnapshot(
    entities=tuple(
        KnownEntity(f"light.x{i}", f"Lampe {i}", f"Raum {i % 20}", "on") for i in range(300)
    )
    + HOUSE.entities,
    areas=tuple(f"Raum {i}" for i in range(20)) + HOUSE.areas,
)

#: The deterministic path exists so a light switch never waits for a model. Even the slowest
#: local model needs hundreds of milliseconds for a first token; this budget is far below that and
#: is a regression guard, not a benchmark result.
MATCH_BUDGET_MS = 25.0
MEASURE_RUNS = 200


def test_matching_costs_no_ai_round_trip() -> None:
    for snapshot in (HOUSE, LARGE_HOUSE):
        resolve_intent("mach das licht im wohnzimmer aus", snapshot)  # warm the caches
        started = time.perf_counter()
        for _ in range(MEASURE_RUNS):
            resolve_intent("mach das licht im wohnzimmer aus", snapshot)
        per_call_ms = (time.perf_counter() - started) / MEASURE_RUNS * 1000
        assert per_call_ms < MATCH_BUDGET_MS, (
            f"{len(snapshot.entities)} entities: {per_call_ms:.3f} ms per match"
        )

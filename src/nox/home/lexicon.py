"""The German and English words the deterministic intent layer understands.

Kept apart from the matcher (`nox.home.intent`) for one reason: this is the file a person edits
when Nox does not understand the way they speak, and it should be readable without understanding
the matching algorithm. Every entry is a lowercase, diacritic-folded token or token sequence -
`normalize` in `nox.home.intent` produces exactly that shape from raw text, so a word written here
the way it is spoken will match.

Nothing here selects a *target*; target matching works on the user's own Home Assistant names.
These tables only answer "which verb was that" and "which kind of device was meant".
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "ALL_QUANTIFIERS",
    "BRIGHTNESS_DOWN",
    "BRIGHTNESS_UP",
    "CLIMATE_WORDS",
    "COLOR_TEMP_COOLER",
    "COLOR_TEMP_WARMER",
    "COVER_CLOSE",
    "COVER_OPEN",
    "COVER_WORDS",
    "DIM_WORDS",
    "DOMAIN_HINTS",
    "MEDIA_NEXT",
    "MEDIA_PAUSE",
    "MEDIA_PLAY",
    "MEDIA_PREVIOUS",
    "REFUSAL_WORDS",
    "SCENE_WORDS",
    "SCRIPT_WORDS",
    "TURN_OFF",
    "TURN_ON",
    "VOLUME_DOWN",
    "VOLUME_UP",
]

#: Device words -> Home Assistant domain. A sentence without one of these can still match when it
#: names an entity directly; it cannot match an area, because "mach das wohnzimmer aus" does not
#: say what to switch off and Nox does not guess.
DOMAIN_HINTS: Final[dict[str, str]] = {
    # light
    "licht": "light",
    "lichter": "light",
    "lampe": "light",
    "lampen": "light",
    "leuchte": "light",
    "leuchten": "light",
    "beleuchtung": "light",
    "deckenlicht": "light",
    "light": "light",
    "lights": "light",
    "lamp": "light",
    "lamps": "light",
    # switch
    "steckdose": "switch",
    "steckdosen": "switch",
    "schalter": "switch",
    "stecker": "switch",
    "switch": "switch",
    "switches": "switch",
    "socket": "switch",
    "plug": "switch",
    "outlet": "switch",
    # media_player
    "musik": "media_player",
    "radio": "media_player",
    "fernseher": "media_player",
    "fernsehen": "media_player",
    "lautsprecher": "media_player",
    "player": "media_player",
    "tv": "media_player",
    "music": "media_player",
    "speaker": "media_player",
    "speakers": "media_player",
    # climate
    "heizung": "climate",
    "heizkorper": "climate",  # "Heizkörper", diacritic-folded
    "thermostat": "climate",
    "klima": "climate",
    "heating": "climate",
    "heater": "climate",
    "radiator": "climate",
    "thermostats": "climate",
    # cover
    "rolladen": "cover",
    "rollladen": "cover",
    "rollo": "cover",
    "rollos": "cover",
    "jalousie": "cover",
    "jalousien": "cover",
    "vorhang": "cover",
    "vorhange": "cover",  # "Vorhänge"
    "blind": "cover",
    "blinds": "cover",
    "shutter": "cover",
    "shutters": "cover",
    "curtain": "cover",
    "curtains": "cover",
}

#: Words that mean "everything of that kind", with no area.
ALL_QUANTIFIERS: Final[frozenset[str]] = frozenset(
    {"alle", "alles", "uberall", "samtliche", "all", "everywhere", "every"}
)

TURN_ON: Final[frozenset[str]] = frozenset(
    {"an", "ein", "anmachen", "anschalten", "einschalten", "aktivier", "aktiviere", "on"}
)
TURN_OFF: Final[frozenset[str]] = frozenset(
    {"aus", "ausmachen", "ausschalten", "abschalten", "deaktivier", "deaktiviere", "off"}
)

DIM_WORDS: Final[frozenset[str]] = frozenset(
    {"dimm", "dimme", "dimmen", "helligkeit", "dim", "brightness"}
)
BRIGHTNESS_UP: Final[frozenset[str]] = frozenset({"heller", "brighter"})
BRIGHTNESS_DOWN: Final[frozenset[str]] = frozenset({"dunkler", "darker", "dimmer"})

COLOR_TEMP_WARMER: Final[frozenset[str]] = frozenset({"warmer", "warmweiss", "warm"})
COLOR_TEMP_COOLER: Final[frozenset[str]] = frozenset({"kalter", "kaltweiss", "kuhler", "cooler"})

SCENE_WORDS: Final[frozenset[str]] = frozenset({"szene", "szenen", "scene", "stimmung"})

MEDIA_PLAY: Final[frozenset[str]] = frozenset(
    {"play", "weiter", "abspielen", "fortsetzen", "resume"}
)
MEDIA_PAUSE: Final[frozenset[str]] = frozenset({"pause", "pausiere", "pausier", "anhalten"})
MEDIA_NEXT: Final[frozenset[str]] = frozenset(
    {"nachster", "nachste", "weiterspringen", "next", "skip"}
)
MEDIA_PREVIOUS: Final[frozenset[str]] = frozenset({"vorheriger", "vorherige", "zuruck", "previous"})
VOLUME_UP: Final[frozenset[str]] = frozenset({"lauter", "louder"})
VOLUME_DOWN: Final[frozenset[str]] = frozenset({"leiser", "quieter"})

CLIMATE_WORDS: Final[frozenset[str]] = frozenset({"grad", "degrees", "celsius", "temperatur"})

COVER_WORDS: Final[frozenset[str]] = frozenset(
    {
        "rolladen",
        "rollladen",
        "rollo",
        "rollos",
        "jalousie",
        "jalousien",
        "vorhang",
        "vorhange",
        "blind",
        "blinds",
        "shutter",
        "shutters",
        "curtain",
        "curtains",
    }
)
COVER_OPEN: Final[frozenset[str]] = frozenset(
    {"hoch", "auf", "offne", "offnen", "open", "up", "raise"}
)
COVER_CLOSE: Final[frozenset[str]] = frozenset(
    {"runter", "zu", "schliess", "schliesse", "schliessen", "close", "down", "lower"}
)

SCRIPT_WORDS: Final[frozenset[str]] = frozenset(
    {"skript", "script", "automation", "automatisierung", "routine"}
)

#: Words that are only ever about securing a building. A sentence containing one of these is
#: refused outright and logged, even when no matching entity exists - the answer must be the same
#: whether or not the user happens to own a smart lock.
#: German separable verbs put the particle at the end ("schließ die Haustür *auf*"), so the verb
#: alone is not enough to recognise one; the nouns below are what every such sentence has in
#: common. A room light genuinely called "Türlicht" is the price, and refusing one light is a much
#: cheaper mistake than opening one door.
REFUSAL_WORDS: Final[frozenset[str]] = frozenset(
    {
        "schloss",
        "turschloss",
        "haustur",
        "wohnungstur",
        "eingangstur",
        "hintertur",
        "terrassentur",
        "tor",
        "door",
        "doors",
        "gate",
        "abschliessen",
        "aufschliessen",
        "zusperren",
        "aufsperren",
        "entriegeln",
        "verriegeln",
        "alarmanlage",
        "alarm",
        "garage",
        "garagentor",
        "hoftor",
        "ventil",
        "lock",
        "unlock",
        "deadbolt",
        "valve",
    }
)

"""Deterministic German/English intent matching for home control - no AI round trip.

"Mach das Licht im Wohnzimmer aus" is not a reasoning problem. It is a verb, a kind of device and
a room. This module owns the verb half - which `home.*` tool a sentence means and with which
arguments - and hands the "which thing" half to `nox.home.targets`.

* :func:`resolve_intent` returns an :class:`IntentMatch` (tool name plus validated-shaped
  arguments), an :class:`IntentRefusal` (the sentence was about a lock, an alarm or a garage door)
  or `None`.
* `None` means "this matcher does not understand it", and the caller is expected to fall back to
  the model. It never guesses: a sentence that names a room but no kind of device
  ("mach das Wohnzimmer aus") is `None`, not a coin flip over the lights.
* Matching is pure and synchronous. It takes a snapshot of entities/areas the caller already has,
  so a match costs no I/O and is trivially testable and measurable.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from nox.home import lexicon
from nox.home.boundary import forbidden_reason
from nox.home.targets import (
    IntentSnapshot,
    KnownEntity,
    best_entity,
    domain_hint,
    first_of,
    fold,
    normalize,
    resolve_targets,
    scene_or_script,
)

__all__ = [
    "Intent",
    "IntentMatch",
    "IntentRefusal",
    "IntentSnapshot",
    "KnownEntity",
    "normalize",
    "resolve_intent",
]

_PERCENT_RE: Final[re.Pattern[str]] = re.compile(r"(\d{1,3})\s*(?:%|prozent|percent)")
_DEGREES_RE: Final[re.Pattern[str]] = re.compile(r"(\d{1,2}(?:[.,]\d)?)\s*(?:grad|degrees|celsius)")
_ON_NUMBER_RE: Final[re.Pattern[str]] = re.compile(r"(?:auf|to|at)\s+(\d{1,2}(?:[.,]\d)?)")


# ---- what a match is ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IntentMatch:
    """A resolved command: which tool to call, with which arguments, and how sure the match is.

    `summary` is plain German, because it is what the dashboard echoes and what a voice reply would
    read out; `tool` and `arguments` are the machine-readable truth and never localized.
    """

    tool: str
    arguments: dict[str, Any]
    entity_ids: tuple[str, ...]
    confidence: float
    summary: str


@dataclass(frozen=True, slots=True)
class IntentRefusal:
    """The sentence was about something Nox never controls; `reason` is logged by the caller."""

    reason: str
    matched_word: str


Intent = IntentMatch | IntentRefusal | None


# ---- reading the numbers and the verb out of a sentence --------------------------------------


def _percent(text: str) -> int | None:
    match = _PERCENT_RE.search(text)
    if match is None:
        return None
    value = int(match.group(1))
    return value if 0 <= value <= 100 else None


def _degrees(text: str) -> float | None:
    match = _DEGREES_RE.search(text) or _ON_NUMBER_RE.search(text)
    if match is None:
        return None
    value = float(match.group(1).replace(",", "."))
    return value if 4.0 <= value <= 35.0 else None


def _refusal(tokens: Sequence[str]) -> IntentRefusal | None:
    """A sentence about a lock, an alarm, a garage door or a valve, before anything else runs."""
    for token in tokens:
        if token in lexicon.REFUSAL_WORDS:
            return IntentRefusal(
                reason=(
                    "locks, alarm panels, garage doors and valves are never controlled by Nox "
                    "(hard boundary, docs/PRIVACY.md)"
                ),
                matched_word=token,
            )
    return None


def _plural(count: int) -> str:
    return "Gerät" if count == 1 else "Geräte"


# ---- one matcher per kind of command ---------------------------------------------------------


def _light_arguments(tokens: Sequence[str], text: str, ids: Sequence[str]) -> dict[str, Any] | None:
    """`home.light` arguments for a brightness or colour-temperature sentence, if it is one."""
    percent = _percent(text)
    if first_of(tokens, lexicon.DIM_WORDS) is not None and percent is not None:
        return {"entity_ids": list(ids), "on": True, "brightness_pct": percent}
    if first_of(tokens, lexicon.BRIGHTNESS_UP) is not None:
        return {"entity_ids": list(ids), "on": True, "brightness_step_pct": 20}
    if first_of(tokens, lexicon.BRIGHTNESS_DOWN) is not None:
        return {"entity_ids": list(ids), "on": True, "brightness_step_pct": -20}
    if first_of(tokens, lexicon.COLOR_TEMP_WARMER) is not None:
        return {"entity_ids": list(ids), "on": True, "color_temp_kelvin": 2700}
    if first_of(tokens, lexicon.COLOR_TEMP_COOLER) is not None:
        return {"entity_ids": list(ids), "on": True, "color_temp_kelvin": 5000}
    return None


def _media_action(tokens: Sequence[str], text: str) -> tuple[str, int | None] | None:
    if first_of(tokens, lexicon.MEDIA_PAUSE) is not None:
        return "pause", None
    if first_of(tokens, lexicon.MEDIA_NEXT) is not None:
        return "next", None
    if first_of(tokens, lexicon.MEDIA_PREVIOUS) is not None:
        return "previous", None
    if first_of(tokens, lexicon.VOLUME_UP) is not None:
        return "volume_up", None
    if first_of(tokens, lexicon.VOLUME_DOWN) is not None:
        return "volume_down", None
    percent = _percent(text)
    if percent is not None:
        return "volume_set", percent
    if first_of(tokens, lexicon.MEDIA_PLAY) is not None:
        return "play", None
    return None


def _cover_action(tokens: Sequence[str]) -> str | None:
    if first_of(tokens, lexicon.COVER_CLOSE) is not None:
        return "close"
    if first_of(tokens, lexicon.COVER_OPEN) is not None:
        return "open"
    return None


def _on_off(tokens: Sequence[str]) -> bool | None:
    """`True` for "an", `False` for "aus", `None` when the sentence says neither.

    `aus` is checked first: "mach das Licht aus" and "schalte das Licht an" both contain a token
    from the other table as a prefix of some German verbs, and switching off is the answer that
    must never be wrong.
    """
    if first_of(tokens, lexicon.TURN_OFF) is not None:
        return False
    if first_of(tokens, lexicon.TURN_ON) is not None:
        return True
    return None


def _cover_intent(tokens: Sequence[str], _text: str, snapshot: IntentSnapshot) -> Intent:
    if first_of(tokens, lexicon.COVER_WORDS) is None:
        return None
    action = _cover_action(tokens)
    if action is None:
        return None
    targets = resolve_targets(tokens, snapshot, "cover")
    if targets is None:
        return None
    ids, label, score = targets
    verb = "auf" if action == "open" else "zu"
    return IntentMatch(
        tool="home.cover",
        arguments={"entity_ids": list(ids), "action": action},
        entity_ids=ids,
        confidence=score,
        summary=f"Rollladen {label}: {verb} ({len(ids)} {_plural(len(ids))})",
    )


def _climate_intent(tokens: Sequence[str], text: str, snapshot: IntentSnapshot) -> Intent:
    hint = domain_hint(tokens)
    if hint != "climate" and first_of(tokens, lexicon.CLIMATE_WORDS) is None:
        return None
    degrees = _degrees(text)
    if degrees is None:
        return None
    targets = resolve_targets(tokens, snapshot, "climate")
    if targets is None:
        return None
    ids, label, score = targets
    return IntentMatch(
        tool="home.climate",
        arguments={"entity_ids": list(ids), "temperature_c": degrees},
        entity_ids=ids,
        confidence=score,
        summary=f"Heizung {label}: {degrees:g} °C ({len(ids)} {_plural(len(ids))})",
    )


def _scene_intent(tokens: Sequence[str], _text: str, snapshot: IntentSnapshot) -> Intent:
    if first_of(tokens, lexicon.SCENE_WORDS) is None:
        return None
    best = scene_or_script(tokens, snapshot, "scene")
    if best is None:
        return None
    entity, score = best
    return IntentMatch(
        tool="home.scene",
        arguments={"entity_id": entity.entity_id},
        entity_ids=(entity.entity_id,),
        confidence=score,
        summary=f"Szene {entity.name} aktivieren",
    )


def _media_intent(tokens: Sequence[str], text: str, snapshot: IntentSnapshot) -> Intent:
    hint = domain_hint(tokens)
    if hint is not None and hint != "media_player":
        return None
    action = _media_action(tokens, text)
    if action is None:
        return None
    verb, volume = action
    targets = resolve_targets(tokens, snapshot, "media_player")
    if targets is None:
        return None
    ids, label, score = targets
    arguments: dict[str, Any] = {"entity_ids": list(ids), "action": verb}
    if volume is not None:
        arguments["volume_pct"] = volume
    return IntentMatch(
        tool="home.media",
        arguments=arguments,
        entity_ids=ids,
        confidence=score,
        summary=f"Wiedergabe {label}: {verb}",
    )


def _light_or_switch_intent(tokens: Sequence[str], text: str, snapshot: IntentSnapshot) -> Intent:
    domain = domain_hint(tokens)
    if domain not in ("light", "switch"):
        return None
    targets = resolve_targets(tokens, snapshot, domain)
    if targets is None:
        return None
    ids, label, score = targets
    if domain == "light":
        adjusted = _light_arguments(tokens, text, ids)
        if adjusted is not None:
            return IntentMatch(
                tool="home.light",
                arguments=adjusted,
                entity_ids=ids,
                confidence=score,
                summary=f"Licht {label}: angepasst ({len(ids)} {_plural(len(ids))})",
            )
    state = _on_off(tokens)
    if state is None:
        return None
    noun = "Licht" if domain == "light" else "Schalter"
    return IntentMatch(
        tool="home.light" if domain == "light" else "home.switch",
        arguments={"entity_ids": list(ids), "on": state},
        entity_ids=ids,
        confidence=score,
        summary=f"{noun} {label}: {'an' if state else 'aus'} ({len(ids)} {_plural(len(ids))})",
    )


def _script_intent(tokens: Sequence[str], _text: str, snapshot: IntentSnapshot) -> Intent:
    if first_of(tokens, lexicon.SCRIPT_WORDS) is None:
        return None
    for domain, tool in (("script", "home.script"), ("automation", "home.automation.trigger")):
        best = scene_or_script(tokens, snapshot, domain)
        if best is None:
            continue
        entity, score = best
        return IntentMatch(
            tool=tool,
            arguments={"entity_id": entity.entity_id},
            entity_ids=(entity.entity_id,),
            confidence=score,
            summary=f"{entity.name} starten",
        )
    return None


#: Domains a bare "mach X an/aus" may resolve to when the sentence names no kind of device.
#: Deliberately only the two that have an unambiguous on/off: "mach den Fernseher an" is a
#: `media_player.turn_on`, which is not the same thing as pressing play, so it is left to the model.
_NAMED_ON_OFF_DOMAINS: Final[frozenset[str]] = frozenset({"light", "switch"})


def _named_entity_intent(tokens: Sequence[str], _text: str, snapshot: IntentSnapshot) -> Intent:
    """ "Schalte die Stehlampe an": an on/off verb plus a thing, with no word for its kind."""
    if domain_hint(tokens) is not None:
        return None
    state = _on_off(tokens)
    if state is None:
        return None
    best = best_entity(tokens, snapshot, _NAMED_ON_OFF_DOMAINS)
    if best is None:
        return None
    entity, score = best
    if forbidden_reason(entity.entity_id, entity.attributes) is not None:
        return None
    tool = "home.light" if entity.domain == "light" else "home.switch"
    return IntentMatch(
        tool=tool,
        arguments={"entity_ids": [entity.entity_id], "on": state},
        entity_ids=(entity.entity_id,),
        confidence=score,
        summary=f"{entity.name}: {'an' if state else 'aus'}",
    )


#: Matchers in the order they are tried. The order is the whole design: a sentence about a
#: thermostat contains "auf", which is also a cover verb, so the more specific matcher runs first.
_MATCHERS: Final[tuple[Callable[[Sequence[str], str, IntentSnapshot], Intent], ...]] = (
    _cover_intent,
    _climate_intent,
    _scene_intent,
    _media_intent,
    _light_or_switch_intent,
    _named_entity_intent,
    _script_intent,
)


def resolve_intent(text: str, snapshot: IntentSnapshot) -> Intent:
    """Map one utterance onto a `home.*` tool call, a refusal, or `None` for "ask the model".

    Pure and synchronous: the same text and snapshot always produce the same answer, and no
    network, disk or clock is involved.
    """
    folded = fold(text)
    tokens = normalize(text)
    if not tokens:
        return None
    refusal = _refusal(tokens)
    if refusal is not None:
        return refusal
    for matcher in _MATCHERS:
        result = matcher(tokens, folded, snapshot)
        if result is not None:
            return result
    return None

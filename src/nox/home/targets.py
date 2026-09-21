"""Finding the thing the user meant: rooms and devices, by their own Home Assistant names.

This is the half of the deterministic intent layer that answers "which thing", while
`nox.home.intent` answers "which verb". Keeping them apart matters because they fail differently:
a verb this package does not know is a missing word in `nox.home.lexicon`, and a name it does not
find is a threshold or a spelling.

Matching is narrow on purpose. Case and diacritics are folded, the utterance is compared to each
candidate name with `difflib` over sliding windows, and a candidate must reach
:data:`MATCH_THRESHOLD` before it counts - which catches "wohnzimer" and "kueche" without ever
turning "Büro" into "Bad". Everything here is pure: no I/O, no clock, no state.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Any, Final

from nox.home import lexicon
from nox.home.boundary import CONTROLLABLE_DOMAINS, domain_of, forbidden_reason

__all__ = [
    "MATCH_THRESHOLD",
    "IntentSnapshot",
    "KnownEntity",
    "best_entity",
    "domain_hint",
    "first_of",
    "fold",
    "normalize",
    "resolve_targets",
    "scene_or_script",
]

#: A candidate name must reach this similarity before it is accepted as the target. Chosen so that
#: one wrong letter in a room name still matches and a different room never does.
MATCH_THRESHOLD: Final[float] = 0.82

#: Candidate names longer than this many words are compared window-by-window; the window search
#: is bounded so a pathological friendly name cannot make matching slow.
MAX_CANDIDATE_WORDS: Final[int] = 6

#: Share of a candidate word's character bigrams that must appear in the sentence before the
#: candidate is compared properly. Low enough that "kueche" still reaches "Küche", high enough that
#: most of a large home's entities are skipped without a `difflib` comparison.
PREFILTER_OVERLAP: Final[float] = 0.5

_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+")

#: Umlauts fold to their spoken two-letter form, everything else to its base letter. `str.translate`
#: first so "ü" becomes "ue" rather than "u" - a Home Assistant name written "Buero" then matches
#: one spoken "Büro".
_FOLD_MAP: Final[dict[int, str]] = {
    ord("ä"): "a",
    ord("ö"): "o",
    ord("ü"): "u",
    ord("ß"): "ss",
}


def normalize(text: str) -> tuple[str, ...]:
    """Lowercase, diacritic-folded word tokens: the shape every `lexicon` table is written in."""
    return tuple(_TOKEN_RE.findall(fold(text)))


def fold(text: str) -> str:
    """Lowercase `text` and strip diacritics, so "Büro" and "Buro" are the same string."""
    lowered = text.lower().translate(_FOLD_MAP)
    decomposed = unicodedata.normalize("NFKD", lowered)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


# ---- what the matcher is given -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KnownEntity:
    """One Home Assistant entity as the matcher needs it: id, name, area and current state."""

    entity_id: str
    name: str
    area: str = ""
    state: str = ""
    attributes: Mapping[str, Any] = field(default_factory=dict)

    @property
    def domain(self) -> str:
        return domain_of(self.entity_id)


@dataclass(frozen=True, slots=True)
class IntentSnapshot:
    """The entities and areas a match is resolved against.

    Built from a `home.list` result; forbidden entities never reach it, because `home.list` filters
    them out one layer down. :func:`resolve_intent` filters again anyway - a boundary that is only
    enforced in one place is enforced nowhere.
    """

    entities: tuple[KnownEntity, ...] = ()
    areas: tuple[str, ...] = ()

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> IntentSnapshot:
        """Build a snapshot from a `home.list` response payload."""
        entities = tuple(
            KnownEntity(
                entity_id=str(row.get("entity_id", "")),
                name=str(row.get("name", "")),
                area=str(row.get("area", "")),
                state=str(row.get("state", "")),
                attributes=dict(row.get("attributes") or {}),
            )
            for row in payload.get("entities", ())
            if isinstance(row, Mapping) and row.get("entity_id")
        )
        areas = tuple(str(a) for a in payload.get("areas", ()) if str(a))
        return cls(entities=entities, areas=areas or _areas_of(entities))


def _areas_of(entities: Iterable[KnownEntity]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for entity in entities:
        if entity.area:
            seen.setdefault(entity.area, None)
    return tuple(seen)


# ---- what the matcher returns -------------------------------------------------------------------


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


# ---- target matching ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Candidate:
    display: str
    tokens: tuple[str, ...]
    is_area: bool
    entity: KnownEntity | None


@dataclass(frozen=True, slots=True)
class _Sentence:
    """The utterance in the three shapes matching needs, computed once per call."""

    tokens: tuple[str, ...]
    words: frozenset[str]
    bigrams: frozenset[str]

    @classmethod
    def of(cls, tokens: Sequence[str]) -> _Sentence:
        return cls(
            tokens=tuple(tokens),
            words=frozenset(tokens),
            bigrams=_utterance_bigrams(tokens),
        )


def _similarity(
    matcher: SequenceMatcher[str], utterance: Sequence[str], candidate: tuple[str, ...]
) -> float:
    """Best `difflib` ratio between the candidate name and any same-length window of the utterance.

    A window rather than the whole sentence: "mach das licht im wohnzimmer aus" against
    "wohnzimmer" scores 0.4 as a whole and 1.0 on the right window, and the second number is the
    one that means something.

    Two things keep this cheap enough to run against every device in a house. The matcher is
    reused and only its *first* sequence changes per window, so `difflib`'s index of the second
    one is built once per candidate instead of once per comparison; and each window is dropped by
    `real_quick_ratio`/`quick_ratio` - both upper bounds on the real ratio - before the real
    comparison runs. Anything below :data:`MATCH_THRESHOLD` therefore comes back as `0.0` rather
    than as its true score, which is all either caller looks at.
    """
    width = min(len(candidate), MAX_CANDIDATE_WORDS)
    matcher.set_seq2(" ".join(candidate[:MAX_CANDIDATE_WORDS]))
    best = 0.0
    for size in {max(1, width - 1), width, width + 1}:
        for start in range(0, max(1, len(utterance) - size + 1)):
            window = " ".join(utterance[start : start + size])
            if not window:
                continue
            matcher.set_seq1(window)
            if matcher.real_quick_ratio() < MATCH_THRESHOLD:
                continue
            if matcher.quick_ratio() < MATCH_THRESHOLD:
                continue
            best = max(best, matcher.ratio())
            if best >= 1.0:
                return best
    return best


@lru_cache(maxsize=4096)
def _bigrams(word: str) -> frozenset[str]:
    """Character bigrams of one word; a one-letter word is its own single "bigram"."""
    if len(word) < 2:
        return frozenset({word})
    return frozenset(word[index : index + 2] for index in range(len(word) - 1))


def _utterance_bigrams(tokens: Sequence[str]) -> frozenset[str]:
    return frozenset().union(*(_bigrams(token) for token in tokens)) if tokens else frozenset()


def _shares_a_word(sentence: _Sentence, candidate: tuple[str, ...]) -> bool:
    """Cheap pre-filter: a candidate no word of which resembles the sentence cannot win.

    Without it every entity in the house is compared against every window of every sentence, which
    is the difference between a sub-millisecond match and a visibly slow one in a large home. The
    test is character bigrams rather than a prefix, because "kueche" and "Küche" share no prefix
    past two letters and are obviously the same room.
    """
    for word in candidate:
        if word in sentence.words:
            return True
        grams = _bigrams(word)
        if len(grams & sentence.bigrams) / len(grams) >= PREFILTER_OVERLAP:
            return True
    return False


def _candidates(snapshot: IntentSnapshot, domain: str | None) -> list[_Candidate]:
    items = [
        _Candidate(display=area, tokens=normalize(area), is_area=True, entity=None)
        for area in snapshot.areas
        if area
    ]
    for entity in snapshot.entities:
        if domain is not None and entity.domain != domain:
            continue
        if not entity.name:
            continue
        items.append(
            _Candidate(
                display=entity.name,
                tokens=normalize(entity.name),
                is_area=False,
                entity=entity,
            )
        )
    return [item for item in items if item.tokens]


def _best_candidate(
    tokens: Sequence[str], snapshot: IntentSnapshot, domain: str | None
) -> tuple[_Candidate, float] | None:
    """The highest-scoring area or entity name, or `None` when nothing reaches the threshold."""
    sentence = _Sentence.of(tokens)
    matcher: SequenceMatcher[str] = SequenceMatcher(autojunk=False)
    best: tuple[_Candidate, float] | None = None
    for candidate in _candidates(snapshot, domain):
        if not _shares_a_word(sentence, candidate.tokens):
            continue
        score = _similarity(matcher, tokens, candidate.tokens)
        if score < MATCH_THRESHOLD:
            continue
        # Ties go to the more specific name: a two-word entity beats the one-word area it sits in.
        if best is None or (score, len(candidate.tokens)) > (best[1], len(best[0].tokens)):
            best = (candidate, score)
    return best


def _best_entity(
    tokens: Sequence[str], snapshot: IntentSnapshot, domains: frozenset[str]
) -> tuple[KnownEntity, float] | None:
    """The highest-scoring *entity* name in one of `domains`, ignoring area names entirely.

    This is what makes "schalte die Stehlampe an" work: the sentence names no kind of device, only
    a thing, and the thing's own domain decides which tool runs.
    """
    sentence = _Sentence.of(tokens)
    matcher: SequenceMatcher[str] = SequenceMatcher(autojunk=False)
    best: tuple[KnownEntity, float] | None = None
    for entity in snapshot.entities:
        if entity.domain not in domains or not entity.name:
            continue
        candidate = normalize(entity.name)
        if not candidate or not _shares_a_word(sentence, candidate):
            continue
        score = _similarity(matcher, tokens, candidate)
        if score < MATCH_THRESHOLD:
            continue
        if best is None or score > best[1]:
            best = (entity, score)
    return best


def _entities_for(
    target: _Candidate, snapshot: IntentSnapshot, domain: str
) -> tuple[KnownEntity, ...]:
    if not target.is_area:
        entity = target.entity
        return (entity,) if entity is not None and entity.domain == domain else ()
    return tuple(
        entity
        for entity in snapshot.entities
        if entity.area == target.display and entity.domain == domain
    )


def _usable(entities: Iterable[KnownEntity]) -> tuple[str, ...]:
    """Entity ids past the boundary check - the second enforcement, on purpose."""
    return tuple(
        entity.entity_id
        for entity in entities
        if forbidden_reason(entity.entity_id, entity.attributes) is None
        and entity.domain in CONTROLLABLE_DOMAINS
    )


def domain_hint(tokens: Sequence[str]) -> str | None:
    """The Home Assistant domain a device word in the sentence names, or `None`."""
    for token in tokens:
        domain = lexicon.DOMAIN_HINTS.get(token)
        if domain is not None:
            return domain
    return None


def first_of(tokens: Sequence[str], words: frozenset[str]) -> str | None:
    """The first token that is in `words`, or `None`."""
    for token in tokens:
        if token in words:
            return token
    return None


def _match_all_of_domain(
    tokens: Sequence[str], snapshot: IntentSnapshot, domain: str
) -> tuple[KnownEntity, ...] | None:
    if first_of(tokens, lexicon.ALL_QUANTIFIERS) is None:
        return None
    return tuple(entity for entity in snapshot.entities if entity.domain == domain)


def resolve_targets(
    tokens: Sequence[str], snapshot: IntentSnapshot, domain: str
) -> tuple[tuple[str, ...], str, float] | None:
    """Entity ids, a label for the summary and the confidence - or `None` when nothing matched."""
    everything = _match_all_of_domain(tokens, snapshot, domain)
    if everything is not None:
        ids = _usable(everything)
        return (ids, "überall", 1.0) if ids else None
    best = _best_candidate(tokens, snapshot, domain)
    if best is None:
        return None
    candidate, score = best
    ids = _usable(_entities_for(candidate, snapshot, domain))
    if not ids:
        return None
    return ids, candidate.display, score


def scene_or_script(
    tokens: Sequence[str], snapshot: IntentSnapshot, domain: str
) -> tuple[KnownEntity, float] | None:
    """One named `scene`, `script` or `automation` entity - never a whole area."""
    best = _best_candidate(tokens, snapshot, domain)
    if best is None or best[0].is_area or best[0].entity is None:
        return None
    entity = best[0].entity
    if forbidden_reason(entity.entity_id, entity.attributes) is not None:
        return None
    return entity, best[1]


def best_entity(
    tokens: Sequence[str], snapshot: IntentSnapshot, domains: frozenset[str]
) -> tuple[KnownEntity, float] | None:
    """See `_best_entity`; the public name this package imports."""
    return _best_entity(tokens, snapshot, domains)

"""Applying a hand-edited layer key by key: one bad value costs that value, not the whole file.

A layer that fails validation is pruned: every value the model rejects is removed on its own,
named by its dotted path, and the rest of the layer applies. Discarding the whole layer instead
used to put a user who had chosen `privacy.mode: offline` back on `balanced`, with a different
`data_dir`, because of one typo elsewhere in the file.

Dropping a value falls back to the layers below it, and for most settings that is harmless. For a
setting that protects the user it is not: the default can be weaker than what the user wrote. Those
settings are listed in `FAIL_CLOSED` with their strictest value, and a rejected one takes that value
instead of the default. The same values apply when a layer cannot be read at all, because then
nothing is known about what the user chose - and "cannot tell" is never "allowed".
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from nox.security.constants import DEFAULT_LOOPBACK_ALLOWLIST
from nox.security.hardlist import HARD_PROHIBITIONS

__all__ = [
    "FAIL_CLOSED",
    "MAX_PRUNE_ROUNDS",
    "Rejection",
    "fail_closed_overlay",
    "prune_invalid",
]

#: Upper bound on validation rounds while pruning one layer. Each round removes at least one
#: value, so a layer with more broken values than this is rejected as a whole.
MAX_PRUNE_ROUNDS = 64

Path = tuple[str | int, ...]
Validate = Callable[[dict[str, Any]], object]
#: `(rejected user value, value of the layers below) -> strictest value`.
StrictValue = Callable[[Any, Any], Any]


def _constant(value: Any) -> StrictValue:
    return lambda _rejected, _below: copy.deepcopy(value)


def _union_of_strings(extra: frozenset[str] | tuple[str, ...] = ()) -> StrictValue:
    """A list that keeps everything below *and* every well-formed entry the user added."""

    def strictest(rejected: Any, below: Any) -> list[str]:
        kept = {str(v) for v in (below or []) if isinstance(v, str)} | set(extra)
        if isinstance(rejected, list):
            kept |= {v for v in rejected if isinstance(v, str) and v.strip()}
        return sorted(kept)

    return strictest


#: Settings whose default may be weaker than what the user wrote, with their strictest value.
FAIL_CLOSED: Mapping[str, StrictValue] = {
    "privacy.mode": _constant("offline"),
    "privacy.capture.microphone": _constant(False),
    "privacy.capture.camera": _constant(False),
    "privacy.capture.screen": _constant(False),
    "privacy.zones": _union_of_strings(),
    "privacy.retention.raw_transcripts_days": _constant(1),
    "privacy.retention.viewer_data_inactive_months": _constant(1),
    "stream.chat.retain_raw_text_days": _constant(0),
    "security.profile": _constant("offline"),
    "security.pin_required_for_security_changes": _constant(True),
    "security.hard_prohibitions": _union_of_strings(frozenset(HARD_PROHIBITIONS)),
    "security.egress_allowlist": _constant([]),
    "security.loopback_allowlist": _constant(list(DEFAULT_LOOPBACK_ALLOWLIST)),
    "security.audit.enabled": _constant(True),
    "logging.pii_filter": _constant(True),
    "remote.enabled": _constant(False),
}


@dataclass(frozen=True, slots=True)
class Rejection:
    """One value removed from a layer: where it was, what was wrong, and what was in it."""

    path: Path
    message: str
    value: Any

    @property
    def dotted(self) -> str:
        return ".".join(str(part) for part in self.path) or "<root>"


def prune_invalid(
    below: dict[str, Any], layer: dict[str, Any], validate: Validate
) -> tuple[dict[str, Any] | None, list[Rejection]]:
    """Remove every value of `layer` that makes `below + layer` invalid, one at a time.

    Returns the pruned layer and what was removed, or None when the layer cannot be reduced to a
    valid one (the caller then rejects it as a whole). `validate` receives the merged mapping and
    raises `ValidationError`.
    """
    current = copy.deepcopy(layer)
    rejections: list[Rejection] = []
    for _ in range(MAX_PRUNE_ROUNDS):
        error = _first_error(validate, _merge(below, current))
        if error is None:
            return current, rejections
        loc, message = error
        path = _narrow(below, current, _existing_prefix(current, loc), error, validate)
        if not path:
            return None, rejections
        rejections.append(Rejection(path=path, message=message, value=_pop(current, path)))
    return None, rejections


def fail_closed_overlay(
    below: Mapping[str, Any], layer: Mapping[str, Any], rejected: list[Rejection] | None
) -> tuple[dict[str, Any], list[str]]:
    """The strict values for the protective settings a rejection touched.

    `rejected=None` means the whole layer was lost, which touches every setting in `FAIL_CLOSED`.
    Returns the overlay (to merge on top of the pruned layer) and the dotted keys it sets.
    """
    overlay: dict[str, Any] = {}
    applied: list[str] = []
    for key, strictest in FAIL_CLOSED.items():
        key_path = tuple(key.split("."))
        hit = (
            _touching(key_path, rejected)
            if rejected is not None
            else _Hit(_lookup(layer, key_path))
        )
        if hit is None:
            continue
        _assign(overlay, key_path, strictest(hit.value, _lookup(below, key_path)))
        applied.append(key)
    return overlay, applied


# ---- helpers ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Hit:
    value: Any


_MISSING = object()


def _touching(key_path: tuple[str, ...], rejected: list[Rejection]) -> _Hit | None:
    """The rejected value at `key_path`, if a rejection removed it or a subtree containing it."""
    for rejection in rejected:
        path = tuple(str(p) for p in rejection.path)
        if path == key_path:
            return _Hit(rejection.value)
        if key_path[: len(path)] == path:  # a whole section containing the key went
            inner = _lookup(rejection.value, key_path[len(path) :])
            return _Hit(None if inner is _MISSING else inner)
    return None


def _merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = _merge(current, value)
        else:
            result[key] = value
    return result


def _first_error(validate: Validate, candidate: dict[str, Any]) -> tuple[Path, str] | None:
    try:
        validate(candidate)
    except ValidationError as exc:
        first = exc.errors()[0]
        return tuple(first["loc"]), str(first["msg"])
    return None


def _has_error(validate: Validate, candidate: dict[str, Any], error: tuple[Path, str]) -> bool:
    try:
        validate(candidate)
    except ValidationError as exc:
        return any((tuple(e["loc"]), str(e["msg"])) == error for e in exc.errors())
    return False


def _existing_prefix(data: Any, loc: Path) -> Path:
    """The longest prefix of `loc` that names something present in `data`."""
    path: list[str | int] = []
    node = data
    for part in loc:
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and isinstance(part, int) and 0 <= part < len(node):
            node = node[part]
        else:
            break
        path.append(part)
    return tuple(path)


def _narrow(
    below: dict[str, Any],
    layer: dict[str, Any],
    path: Path,
    error: tuple[Path, str],
    validate: Validate,
) -> Path:
    """Descend from a section to the one entry whose removal clears `error`.

    A model-level check reports its section rather than the offending key; removing the whole
    section would drop unrelated - possibly protective - settings with it.
    """
    node = _lookup(layer, path) if path else layer
    while isinstance(node, dict) and node:
        culprit: str | None = None
        for key in list(node):
            trial = copy.deepcopy(layer)
            _pop(trial, (*path, key))
            if not _has_error(validate, _merge(below, trial), error):
                culprit = key
                break
        if culprit is None:
            return path
        path = (*path, culprit)
        node = node[culprit]
    return path


def _lookup(data: Any, path: tuple[str | int, ...]) -> Any:
    node = data
    for part in path:
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        elif isinstance(node, list) and isinstance(part, int) and 0 <= part < len(node):
            node = node[part]
        else:
            return _MISSING
    return node


def _pop(data: dict[str, Any], path: Path) -> Any:
    parent: Any = data
    for part in path[:-1]:
        parent = parent[part]
    return parent.pop(path[-1])


def _assign(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = data
    for part in path[:-1]:
        node = node.setdefault(part, {})
    node[path[-1]] = value

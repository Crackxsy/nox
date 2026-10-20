"""The condition of a watcher: data, not code.

A watcher never runs anything a model wrote. Its condition is four fields - a path into the tool's
answer, an optional text anchor, an operator from a closed list and a value - and this module is the
whole interpreter for them. Nothing here evaluates, imports or formats a string a model chose.

* `path` - dotted keys and list indexes into the answer: `structured.delay_min`, `items.0.price`,
  `content`. Empty means the whole answer.
* `near` - for text: only what follows this literal anchor, up to the end of its line (at most
  `NEAR_CHARS`). "RB 40" on a timetable page reads that train's row and nothing else.
* `op` - `lt` / `gt` compare numbers (the first number in a text), `contains` looks for the value
  in a text or a list (case-insensitive), `changed` fires when the value differs from the one last
  reported.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Any, Final

__all__ = ["MISSING", "NEAR_CHARS", "Op", "evaluate", "number", "preview", "resolve", "value_hash"]

NEAR_CHARS = 160
MAX_PATH_SEGMENTS = 12


class Op(StrEnum):
    LT = "lt"
    GT = "gt"
    CONTAINS = "contains"
    CHANGED = "changed"


class _Missing:
    def __repr__(self) -> str:
        return "MISSING"


MISSING: Final = _Missing()

_NUMBER = re.compile(r"[-+]?\d+(?:[.,]\d+)?")
_SEGMENT = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def parse_path(path: str) -> list[str]:
    """Split and check a path; raises `ValueError` for anything that is not plain segments."""
    text = path.strip()
    if not text:
        return []
    segments = text.split(".")
    if len(segments) > MAX_PATH_SEGMENTS or not all(_SEGMENT.fullmatch(s) for s in segments):
        raise ValueError(f"not a usable path: {path!r} (dotted keys and list indexes only)")
    return segments


def resolve(data: Any, path: str, near: str = "") -> Any:
    """The value at `path` (then after `near`), or `MISSING`."""
    current = data
    for segment in parse_path(path):
        if isinstance(current, dict):
            if segment not in current:
                return MISSING
            current = current[segment]
        elif isinstance(current, list) and segment.isdigit():
            index = int(segment)
            if index >= len(current):
                return MISSING
            current = current[index]
        else:
            return MISSING
    if near:
        text = current if isinstance(current, str) else json.dumps(current, ensure_ascii=False)
        position = text.lower().find(near.lower())
        if position < 0:
            return MISSING
        rest = text[position + len(near) :]
        return rest.split("\n", 1)[0][:NEAR_CHARS].strip()
    return current


def number(value: Any) -> float | None:
    """A number, or the first number written in a text (`"12,99 EUR"` -> 12.99)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        match = _NUMBER.search(value)
        if match is not None:
            return float(match.group(0).replace(",", "."))
    return None


def value_hash(value: Any) -> str:
    blob = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def preview(value: Any, limit: int = 120) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def evaluate(op: Op, observed: Any, expected: Any, baseline: str | None) -> bool:
    """Whether the condition holds for `observed`. `baseline` is the hash last reported."""
    if observed is MISSING:
        return False
    if op is Op.CHANGED:
        return baseline is not None and value_hash(observed) != baseline
    if op is Op.CONTAINS:
        needle = str(expected).strip().lower()
        if not needle:
            return False
        if isinstance(observed, list):
            return any(needle in str(item).lower() or item == expected for item in observed)
        text = observed if isinstance(observed, str) else json.dumps(observed, ensure_ascii=False)
        return needle in text.lower()
    left, right = number(observed), number(expected)
    if left is None or right is None:
        return False
    return left < right if op is Op.LT else left > right

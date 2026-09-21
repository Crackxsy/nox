"""Minimal YAML-frontmatter read/render for vault notes (Data Model Layer 3).

Deliberately independent from `nox.pm.vault_repo`'s note helpers (same shape, different owner
package) to avoid a cross-epic import; `nox.memory` and `nox.pm` may evolve their frontmatter
handling separately. Frontmatter is parsed with `yaml.safe_load`; the body is returned untouched.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(?P<fm>.*?)\r?\n---\r?\n?(?P<body>.*)\Z", re.DOTALL)


class FrontmatterError(ValueError):
    """No frontmatter block, or the block is not a YAML mapping."""


@dataclass(frozen=True, slots=True)
class VaultNote:
    path: Path
    frontmatter: dict[str, Any]
    body: str
    raw_hash: str
    raw_text: str


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_note(path: Path, text: str | None = None) -> VaultNote:
    """Parse `path` (or `text` if given, to avoid a second read). No frontmatter -> empty dict, the
    whole file is the body (many vault notes, especially non-Nox ones, have none)."""
    raw = path.read_text(encoding="utf-8") if text is None else text
    match = _FRONTMATTER_RE.match(raw)
    if match is None:
        return VaultNote(path=path, frontmatter={}, body=raw, raw_hash=hash_text(raw), raw_text=raw)
    fm_text = match.group("fm")
    body = match.group("body")
    loaded = yaml.safe_load(fm_text)
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise FrontmatterError(f"{path}: frontmatter is not a mapping")
    return VaultNote(
        path=path, frontmatter=loaded, body=body, raw_hash=hash_text(raw), raw_text=raw
    )


def render_note(frontmatter: dict[str, Any], body: str) -> str:
    if not frontmatter:
        return body
    fm_text = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).rstrip("\n")
    return f"---\n{fm_text}\n---\n{body}"


def is_ignored(note: VaultNote) -> bool:
    """`nox: { ignore: true }`."""
    nox = note.frontmatter.get("nox")
    return isinstance(nox, dict) and bool(nox.get("ignore", False))


def owner(note: VaultNote) -> str | None:
    value = note.frontmatter.get("owner")
    return str(value) if value else None

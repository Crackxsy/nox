"""Readability guard: no references a public reader cannot resolve.

Run by CI (`python scripts/check_planning_ids.py`) and by the unit tests. It scans the text files
under `src/`, `tests/` and `docs/` for identifiers of private planning and review documents:
section numbers of a specification that is not in this repository, numbered decisions, review
finding ids, delivery-step ids.

Such a reference explains nothing to someone who only has the repository. Write the rule itself
in plain words instead (why the code does what it does), or link a public anchor such as
`docs/ARCHITECTURE-NODES.md` or `docs/SECURITY.md`. Where a test comes from - which review found
the gap - belongs in the commit message.

A match that is not such a reference (a voice preset called like a finding id, a benchmark's own
data) goes into `scripts/planning_ids_allowlist.txt` with a reason. An allowlist entry that no
longer matches anything fails the check too, so the list stays reviewed.

The patterns are assembled from pieces so this file and its test do not trip the scan.
"""

from __future__ import annotations

import fnmatch
import re
import sys
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("src", "tests", "docs")
SUFFIXES = {
    ".css", ".html", ".js", ".json", ".md", ".ps1", ".py", ".sh", ".sql", ".toml", ".ts", ".tsx",
    ".txt", ".yaml", ".yml",
}  # fmt: skip
ALLOWLIST = ROOT / "scripts" / "planning_ids_allowlist.txt"

#: One finding or step id: one or two capitals, then a number (`H1`, `N19`, `R3b`) or - after a
#: dash - a letter (`M-c`).
_ID = r"[A-Z]{1,2}(?:\d{1,2}[a-g]?|-[a-d])"
_SECTION = chr(0xA7)  # the section sign, kept out of this file's own text
_MISREAD = chr(0xC2)  # what precedes it when UTF-8 was read as Latin-1

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "a section of a specification that is not in this repository",
        re.compile(rf"[Ss]pec(?: v[\d.]+| wave)?(?: [A-Z])? ?(?:{_MISREAD})?{_SECTION}"),
    ),
    ("a dotted section number", re.compile(rf"{_SECTION} ?\d+\.\d+")),
    ("a numbered owner decision", re.compile(r"\bowner " + r"decision,? [A-Z]?\d")),
    ("a review finding id", re.compile(rf"\b[Rr]e-?view {_ID}\b|\b[Rr]eview {_ID}\b")),
    ("a review round", re.compile(r"\b[Rr]eview(?: panel)?,? " + r"round \d")),
    (
        "an id in parentheses",
        re.compile(rf"(?<!\w)\((?:{_ID})(?:(?:, ?|/|-| and )(?:{_ID}|\d{{1,2}}[a-g]?))*[),]"),
    ),
    ("an acceptance criterion", re.compile("acceptance " + "criterion")),
    ("a delivery-step id", re.compile(r"\bC(?:[3-9]|10)\b|\bO[1-6]\b")),
    (
        "an id opening a comment or docstring",
        re.compile(
            rf'(?:"""|#:? (?:-+ )?)(?:[Rr]eview )?{_ID}'
            rf"(?:(?: ?/ ?|, ?| )(?:{_ID}|[a-g](?:-[a-g])?))*"
            r"(?: \(\w+\))?(?:[:.]|\s*$)"
        ),
    ),
    ("a wrongly decoded section sign", re.compile(_MISREAD + _SECTION)),
)


class Allowed(NamedTuple):
    """One allowlist line: a path pattern, the text the line must contain, and why."""

    path: str
    text: str
    reason: str


def allowlist(path: Path = ALLOWLIST) -> list[Allowed]:
    entries: list[Allowed] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split(" :: ")]
        if len(parts) != 3 or not all(parts):
            raise ValueError(f"{path.name}:{number}: expected `path :: text :: reason`")
        entries.append(Allowed(*parts))
    return entries


def files(root: Path = ROOT) -> list[Path]:
    found: list[Path] = []
    for top in SCANNED:
        base = root / top
        if base.is_dir():
            found += [
                p
                for p in base.rglob("*")
                if p.is_file() and p.suffix in SUFFIXES and "node_modules" not in p.parts
            ]
    return sorted(found)


def hits(root: Path = ROOT) -> list[tuple[str, int, str, str]]:
    """Every match as (path, line number, line, what it is)."""
    found: list[tuple[str, int, str, str]] = []
    for path in files(root):
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            for what, pattern in PATTERNS:
                if pattern.search(line):
                    found.append((rel, number, line.strip(), what))
                    break
    return found


def problems(root: Path = ROOT, allowed: list[Allowed] | None = None) -> list[str]:
    entries = allowlist() if allowed is None else allowed
    used: set[Allowed] = set()
    out: list[str] = []
    for rel, number, line, what in hits(root):
        entry = next((e for e in entries if fnmatch.fnmatch(rel, e.path) and e.text in line), None)
        if entry is not None:
            used.add(entry)
            continue
        out.append(f"{rel}:{number}: {what}: {line[:120]}")
    for entry in entries:
        if entry not in used:
            out.append(f"allowlist: `{entry.path} :: {entry.text}` matches nothing any more")
    return out


def main() -> int:
    found = problems()
    for line in found:
        print(f"::error::{line}")
    if found:
        print(
            "Say the rule in plain words or link a public anchor (docs/ARCHITECTURE-NODES.md, "
            "docs/SECURITY.md); a false positive goes into scripts/planning_ids_allowlist.txt."
        )
    else:
        print("clean: no private planning or review ids in src/, tests/ and docs/")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())

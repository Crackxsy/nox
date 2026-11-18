"""Readability guard: no references a public reader cannot resolve.

Run by CI (`python scripts/check_planning_ids.py`) and by the unit tests. It scans the text files
of the whole repository (code, tests, docs, UI, installers, the OS image, benchmarks, workflows,
configuration, the changelog) for identifiers of private planning and review documents: section
numbers of a specification that is not in this repository, numbered decisions, review finding
ids, delivery waves and their step ids, epic and story numbers.

Such a reference explains nothing to someone who only has the repository. Write the rule itself
in plain words instead (why the code does what it does), or link a public anchor such as
`docs/ARCHITECTURE-NODES.md` or `docs/SECURITY.md`. Where a test comes from - which review found
the gap - belongs in the commit message.

A match that is not such a reference (a voice preset called like a finding id, a fixture in the
format of the user's own notes) goes into `scripts/planning_ids_allowlist.txt` with a reason. An
allowlist entry that no longer matches anything fails the check too, so the list stays reviewed.

The patterns are assembled from pieces so this file and its test do not trip the scan.
"""

from __future__ import annotations

import fnmatch
import re
import sys
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
SCANNED = (
    "src", "tests", "docs", "ui", "installer", "os", "evals", ".github", "config", "plugins",
    "scripts",
)  # fmt: skip
#: Single files at the top of the repository that are scanned as well.
SCANNED_TOP = ("README.md", "CONTRIBUTING.md", "CHANGELOG.md", "SECURITY.md")
#: Folders and files that are generated or somebody else's text, which nothing here can rephrase.
SKIPPED_FOLDERS = {"node_modules", ".venv", "dist", "build", "__pycache__", ".git", ".hypothesis"}
SKIPPED_NAMES = {"package-lock.json", "uv.lock", "pnpm-lock.yaml", "yarn.lock"}
#: Binary formats. Every other file is read as text unless it holds a NUL byte (see `is_text`).
BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".icns", ".svg", ".wav", ".mp3", ".ogg",
    ".mp4", ".webm", ".onnx", ".pt", ".bin", ".gz", ".zip", ".woff", ".woff2", ".ttf", ".pdf",
    ".sqlite", ".db", ".pyc", ".map",
}  # fmt: skip
ALLOWLIST = ROOT / "scripts" / "planning_ids_allowlist.txt"

#: One finding or step id: one or two capitals, then a number (`H1`, `N19`, `R3b`) or - after a
#: dash - a letter (`M-c`).
_ID = r"[A-Z]{1,2}(?:\d{1,2}[a-g]?|-[a-d])"
_SECTION = chr(0xA7)  # the section sign, kept out of this file's own text
_MISREAD = chr(0xC2)  # what precedes it when UTF-8 was read as Latin-1
#: A delivery step of the roadmap: the letter C with a number up to ten, or O with a number up to
#: eight. Not the name of a range of control characters, which follows the same spelling.
_STEP = r"(?<![\w/.-])(?:C(?:[1-9]|10)|O[1-8])\b(?! controls?\b)"
#: A delivery wave: the word, then a capital from the roadmap's range of letters.
_WAVE = r"\b(?:[Ww]" + r"ave|Welle|Wellen) [A-F]\b"

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "a section of a specification that is not in this repository",
        re.compile(
            rf"[Ss]pec(?: v[\d.]+| wave)?(?: [A-Z])? ?(?:{_MISREAD})?{_SECTION}"
            rf"|[Ss]pec [A-Z][\w-]+ ?{_SECTION}"
            r"|[Ss]pec UX-" + "Neubau"
        ),
    ),
    ("a dotted section number", re.compile(rf"{_SECTION} ?\d+\.\d+")),
    ("a decision of the owner", re.compile(r"\bowner " + r"decision")),
    ("a review finding id", re.compile(rf"\b[Rr]e-?view {_ID}\b|\b[Rr]eview {_ID}\b")),
    (
        "a numbered review finding",
        re.compile(r"\b[Rr]eview " + r"findings? ?(?:#?\d|\))|\([Rr]eview " + r"findings?\)"),
    ),
    ("a review round", re.compile(r"\b[Rr]eview(?: panel)?,? " + r"round \d")),
    ("a panel of reviewers", re.compile(r"\b[Rr]eview " + "panel")),
    ("a delivery wave", re.compile(_WAVE + r"|\b[Ss]pec " + "wave")),
    ("an epic or story number", re.compile(r"\bEPIC-\d+|\bST-\d+-\d+")),
    (
        "an id in parentheses",
        re.compile(rf"(?<!\w)\((?:{_ID})(?:(?:, ?|/|-| and )(?:{_ID}|\d{{1,2}}[a-g]?))*[),]"),
    ),
    ("a numbered criterion of acceptance", re.compile("acceptance " + "criterion")),
    ("a delivery-step id", re.compile(_STEP)),
    (
        "a step id after `step`, `UX` or `spec`",
        re.compile(r"\b(?:[Ss]teps?|UX|[Ss]pec) [A-Z]\d{1,2}\b"),
    ),
    (
        "a step id opening a quoted text",
        re.compile(r"""(?<=["'`])[CDEFO]\d{1,2}(?:/[CDEFO]?\d{1,2})*(?=:\s)"""),
    ),
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


def is_text(path: Path) -> bool:
    """A file to scan: not a known binary format, a lockfile or a file with a NUL byte up front."""
    if path.suffix.lower() in BINARY_SUFFIXES or path.name in SKIPPED_NAMES:
        return False
    try:
        with path.open("rb") as handle:
            return b"\x00" not in handle.read(4096)
    except OSError:
        return False


def files(root: Path = ROOT) -> list[Path]:
    found = [root / name for name in SCANNED_TOP if (root / name).is_file()]
    for top in SCANNED:
        base = root / top
        if base.is_dir():
            found += [
                p
                for p in base.rglob("*")
                if p.is_file()
                and not SKIPPED_FOLDERS.intersection(p.relative_to(root).parts)
                and p != ALLOWLIST  # it quotes the lines it excuses
                and is_text(p)
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
        print("clean: no private planning or review ids in the repository")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())

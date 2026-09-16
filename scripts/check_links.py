"""Verify that every relative link in the repository's Markdown files resolves to a real file.

External links (http/https/mailto) and pure anchors are ignored - this only catches the class of
bug that actually happens: a doc pointing at a file that was renamed, moved or never existed.

Usage:
    python scripts/check_links.py           # report and exit 1 on the first broken link
    python scripts/check_links.py --list    # also print every link that was checked
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]

SCAN_DIRS = ("docs", ".github")
SKIP_DIRS = {".git", ".venv", "node_modules", "build", "dist", "__pycache__", ".mypy_cache"}

# [text](target) - target may be wrapped in <>; ignores image/link titles after a space.
INLINE_LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
# [label]: target  (reference-style definitions)
REFERENCE_LINK = re.compile(r"^\[[^\]]+\]:\s*<?([^\s>]+)>?", re.MULTILINE)

EXTERNAL = re.compile(r"^(https?:|mailto:|tel:|ftp:|data:|#)", re.IGNORECASE)

# Fenced code blocks and inline code spans are not prose: a shell snippet may well contain
# something that looks like `[a](b)` without being a link.
CODE_FENCE = re.compile(r"^(```|~~~).*?^\1", re.MULTILINE | re.DOTALL)
CODE_SPAN = re.compile(r"`[^`\n]*`")


def markdown_files() -> list[Path]:
    files = sorted(p for p in ROOT.glob("*.md") if p.is_file())
    for name in SCAN_DIRS:
        base = ROOT / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            if SKIP_DIRS.isdisjoint(part for part in path.parts):
                files.append(path)
    return files


def targets(text: str) -> list[str]:
    text = CODE_SPAN.sub("``", CODE_FENCE.sub("", text))
    found = INLINE_LINK.findall(text)
    found.extend(REFERENCE_LINK.findall(text))
    return found


def check(path: Path, show_all: bool) -> list[str]:
    problems: list[str] = []
    text = path.read_text(encoding="utf-8")
    for raw in targets(text):
        if EXTERNAL.match(raw):
            continue
        target = unquote(raw.split("#", 1)[0]).strip()
        if not target:
            continue
        resolved = (path.parent / target).resolve()
        rel = path.relative_to(ROOT).as_posix()
        if not resolved.exists():
            problems.append(f"{rel}: broken link -> {raw}")
        elif show_all:
            print(f"  ok  {rel} -> {target}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="print every checked link")
    args = parser.parse_args()

    files = markdown_files()
    problems: list[str] = []
    for path in files:
        problems.extend(check(path, args.list))

    if problems:
        print(f"check_links: {len(problems)} broken link(s) in {len(files)} Markdown file(s):")
        for problem in problems:
            print(f"  {problem}")
        return 1

    print(f"check_links: OK - all relative links resolve ({len(files)} Markdown files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

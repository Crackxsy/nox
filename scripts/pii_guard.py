"""Personal-data guard: fails when a private term (the maintainer's real name, home town, employer,
e-mail local part, home-directory name, ...) appears anywhere in the tracked files - without the
repository ever containing those terms in readable or guessable form.

How it works: every tracked text file and every tracked path is split into tokens - words
(letters and digits), the camelCase parts of a word, and dotted/dashed runs such as an e-mail
local part (`first.last2024`) or a host name. Each token is normalised (NFKC, case-folded) and
fingerprinted as HMAC-SHA256(key, token). A hit against `docs/pii_guard_hashes.txt` is a finding.
Findings report file, line and column and a fingerprint prefix - never the token, so a red CI
log does not publish the term either.

The key is secret (`PII_GUARD_KEY`: a GitHub Actions secret in CI, the maintainer's credential
store locally). Without it the committed fingerprints cannot be checked against a list of guessed
words, which a plain salted hash would allow. Without a key the guard reports "skipped" and passes,
unless `--require-key` is given (CI does that for everything but pull requests from forks, which
never receive secrets).

Key lookup: the `PII_GUARD_KEY` environment variable, else (outside CI) the `keyring` entry
service `nox-dev`, name `pii_guard_key`, if the `keyring` package is installed.

Adding a term - the term (and, if no key is found, the key first) is read from stdin, never from
the command line, so neither ends up in shell history or process listings:

    python scripts/pii_guard.py --add-term
    # type the term, Enter (input is not echoed when stdin is a terminal)

Usage:
    python scripts/pii_guard.py                         # report findings
    python scripts/pii_guard.py --check                 # exit 1 on any finding
    python scripts/pii_guard.py --check --require-key   # ... and on a missing key (CI)

Standard library only (`keyring` is optional), so it runs before any dependency is installed.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import os
import re
import subprocess
import sys
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HASHES_PATH = REPO_ROOT / "docs" / "pii_guard_hashes.txt"
KEY_ENV = "PII_GUARD_KEY"
KEYRING_SERVICE = "nox-dev"
KEYRING_NAME = "pii_guard_key"

#: Letters and digits only - `C:\Users\someone\x` yields `someone`, `snake_case` yields both parts.
_WORD_RE = re.compile(r"[^\W_]+")
#: Runs that keep dots, dashes, plus and underscores - an e-mail local part, a host, an identifier.
_COMPOUND_RE = re.compile(r"[\w.+-]+")
#: camelCase / PascalCase parts of one word: `MyHomeTown` -> My, Home, Town; `HTTPServer` -> HTTP,
#: Server.
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_EDGE_PUNCT = ".+-_"
_BINARY_SNIFF = 8192


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    line: int  # 0 = the path itself
    column: int
    digest: str

    def render(self) -> str:
        where = f"{self.path}:{self.line}:{self.column}" if self.line else f"{self.path} (path)"
        return f"{where}: private term (fingerprint {self.digest[:12]})"


@dataclass(frozen=True, slots=True)
class Fingerprints:
    key: bytes
    digests: frozenset[str]

    def digest(self, token: str) -> str:
        return hmac.new(self.key, token.encode("utf-8"), hashlib.sha256).hexdigest()


def normalise(token: str) -> str:
    return unicodedata.normalize("NFKC", token).casefold()


def tokens(text: str) -> Iterator[tuple[str, int]]:
    """Yield (normalised token, offset) for every word, camelCase part and compound run."""
    for match in _WORD_RE.finditer(text):
        word = match.group()
        yield normalise(word), match.start()
        for part in _CAMEL_RE.finditer(word):
            if part.group() != word:
                yield normalise(part.group()), match.start() + part.start()
    for match in _COMPOUND_RE.finditer(text):
        run = match.group().strip(_EDGE_PUNCT)
        if run and not _WORD_RE.fullmatch(run):
            yield normalise(run), match.start() + match.group().index(run[0])


def is_single_token(term: str) -> bool:
    """A term can only ever match if the tokenizer produces it whole."""
    normalised = normalise(term.strip())
    return bool(normalised) and any(tok == normalised for tok, _ in tokens(normalised))


def load_digests(path: Path = HASHES_PATH) -> frozenset[str]:
    digests: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not re.fullmatch(r"[0-9a-f]{64}", line):
            raise ValueError(f"{path}: unexpected line {raw!r}")
        digests.add(line)
    return frozenset(digests)


def key_from_keyring() -> str | None:
    if os.environ.get("CI"):
        return None  # CI gets the key from the environment only, never from a desktop keyring
    try:
        import keyring  # optional: Windows Credential Manager, macOS Keychain, Secret Service

        return keyring.get_password(KEYRING_SERVICE, KEYRING_NAME)
    except Exception:  # no package, no backend, locked store: no key, same as not configured
        return None


def find_key() -> str | None:
    key = os.environ.get(KEY_ENV, "").strip()
    return key or (key_from_keyring() or "").strip() or None


def scan_text(path: str, text: str, prints: Fingerprints, cache: dict[str, str]) -> list[Finding]:
    findings: list[Finding] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for token, column in tokens(line):
            digest = cache.get(token)
            if digest is None:
                digest = cache[token] = prints.digest(token)
            if digest in prints.digests:
                findings.append(Finding(path, line_no, column + 1, digest))
    return findings


def scan_path(path: str, prints: Fingerprints, cache: dict[str, str]) -> list[Finding]:
    digests = {cache.setdefault(t, prints.digest(t)) for t, _ in tokens(path)}
    return [Finding(path, 0, 0, d) for d in sorted(digests) if d in prints.digests]


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True
    ).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def read_text(file: Path) -> str | None:
    try:
        data = file.read_bytes()
    except OSError:  # listed by git but gone from the working tree (deleted, not yet staged)
        return None
    if b"\0" in data[:_BINARY_SNIFF]:
        return None
    return data.decode("utf-8", errors="replace")


def scan_repo(
    root: Path, prints: Fingerprints, files: Iterable[str] | None = None
) -> list[Finding]:
    cache: dict[str, str] = {}
    findings: list[Finding] = []
    for rel in tracked_files(root) if files is None else files:
        findings.extend(scan_path(rel, prints, cache))
        text = read_text(root / rel)
        if text is not None:
            findings.extend(scan_text(rel, text, prints, cache))
    return findings


def add_term(term: str, key: str, path: Path = HASHES_PATH) -> bool:
    """Append the term's fingerprint; False if already listed, ValueError if it can never match."""
    if not is_single_token(term):
        raise ValueError(
            "not a single token (the guard splits on spaces, '@', '/', ...) - add its parts "
            "one by one, e.g. an e-mail address as its local part"
        )
    prints = Fingerprints(key.encode("utf-8"), load_digests(path))
    digest = prints.digest(normalise(term.strip()))
    if digest in prints.digests:
        return False
    text = path.read_text(encoding="utf-8")
    path.write_text(text.rstrip("\n") + f"\n{digest}\n", encoding="utf-8", newline="\n")
    return True


def _read_secret(prompt: str) -> str:
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    return sys.stdin.readline().rstrip("\r\n")


def main(argv: list[str] | None = None, *, key_source: Callable[[], str | None] = find_key) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n", 1)[0])
    parser.add_argument("--check", action="store_true", help="exit 1 on any finding")
    parser.add_argument(
        "--require-key", action="store_true", help="exit 1 when no key is available (CI)"
    )
    parser.add_argument(
        "--add-term", action="store_true", help="fingerprint one term read from stdin into the list"
    )
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--hashes", type=Path, default=HASHES_PATH, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    key = key_source()

    if args.add_term:
        if key is None:
            key = _read_secret(f"{KEY_ENV} (not echoed): ").strip()
        if not key:
            print("pii_guard: no key given.", file=sys.stderr)
            return 2
        try:
            added = add_term(_read_secret("Term (not echoed): "), key, args.hashes)
        except ValueError as exc:
            print(f"pii_guard: {exc}", file=sys.stderr)
            return 2
        print("pii_guard: added." if added else "pii_guard: already listed.")
        return 0

    if key is None:
        print(f"pii_guard: skipped (no key: set {KEY_ENV}).")
        return 1 if args.require_key else 0
    prints = Fingerprints(key.encode("utf-8"), load_digests(args.hashes))
    findings = scan_repo(args.root, prints)
    for finding in findings:
        print(finding.render())
    print(f"pii_guard: {len(findings)} finding(s).")
    return 1 if args.check and findings else 0


if __name__ == "__main__":
    sys.exit(main())

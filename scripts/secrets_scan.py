"""Secrets scan of the working tree and the full git history (ST-21-07, Spec v1.0 Public Release
§9). No network calls; runs `git` as a subprocess against the current repository.

Patterns: generic high-entropy tokens, `sk-...` (OpenAI/Anthropic-style API keys), `oauth:...`
(Twitch IRC OAuth tokens), `ghp_...` (GitHub tokens), a Telegram bot-token shape
(`\\d{8,10}:[A-Za-z0-9_-]{35}`), and `password=...` assignments. Findings on a path matching
`docs/scan_policy_secrets.yaml`'s `exclude_paths` are skipped entirely; findings matching
`accepted_findings` are reported but do not fail `--check`.

Usage:
    python scripts/secrets_scan.py                 # scans tree + history, prints a report
    python scripts/secrets_scan.py --check          # exits 1 on any unaccepted finding
    python scripts/secrets_scan.py --tree-only       # skip the (slower) history scan
"""

from __future__ import annotations

import argparse
import fnmatch
import math
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = REPO_ROOT / "docs" / "scan_policy_secrets.yaml"

# Named patterns: (label, compiled regex). Matched against individual lines.
NAMED_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("anthropic_or_openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("twitch_irc_oauth", re.compile(r"\boauth:[A-Za-z0-9]{10,}\b")),
    ("github_token", re.compile(r"\bghp_[A-Za-z0-9]{20,}\b")),
    ("telegram_bot_token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    # A quoted string literal assigned to `password` (`password = "..."`), or a `password=` query
    # parameter - not a bare `password: str` type hint or `password = <identifier>`/`await ...`,
    # which is ordinary code, not a literal credential.
    ("password_assignment", re.compile(r"\bpassword\s*=\s*['\"][^'\"\n]{3,}['\"]", re.IGNORECASE)),
    ("password_in_url", re.compile(r"[?&]password=[^&\s'\"]{3,}", re.IGNORECASE)),
]

# Generic high-entropy candidate tokens: mixed alphanumeric (+ base64/url-safe punctuation),
# 24-100 chars. Pure lowercase/uppercase hex of any length is excluded (almost always a git/content
# hash, not a secret) by _looks_like_hash below.
_ENTROPY_TOKEN_RE = re.compile(r"[A-Za-z0-9+/_=-]{24,100}")
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
# Lines that are near-certainly hash/lockfile noise even outside excluded paths.
_NOISE_LINE_RE = re.compile(
    r"sha256|sha512|sha1|integrity|resolved\"|checksum|content-hash|\"hash\"", re.IGNORECASE
)
ENTROPY_THRESHOLD = 4.3


def _looks_like_hash(token: str) -> bool:
    return bool(_HEX_RE.match(token))


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    length = len(s)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


@dataclass(frozen=True, slots=True)
class Finding:
    origin: str  # "tree" | "history"
    path: str
    label: str
    line_no: int | None
    excerpt: str
    commit: str | None = None
    accepted: bool = False
    accept_reason: str = ""


def load_policy(path: Path = POLICY_PATH) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {
        "exclude_paths": [str(p) for p in data.get("exclude_paths", [])],
        "accepted_findings": [
            {
                "path": str(e["path"]),
                "pattern": str(e["pattern"]),
                "reason": str(e.get("reason", "")).strip(),
            }
            for e in data.get("accepted_findings", [])
        ],
    }


def is_excluded(path: str, policy: dict[str, Any]) -> bool:
    posix = path.replace("\\", "/")
    return any(fnmatch.fnmatch(posix, pattern) for pattern in policy["exclude_paths"])


def find_acceptance(path: str, label_or_text: str, policy: dict[str, Any]) -> str | None:
    posix = path.replace("\\", "/")
    for entry in policy["accepted_findings"]:
        if not fnmatch.fnmatch(posix, entry["path"]):
            continue
        if entry["pattern"] == "*" or entry["pattern"] in label_or_text:
            return entry["reason"] or "accepted (no reason given)"
    return None


def _scan_line(line: str) -> list[tuple[str, str]]:
    """Return [(label, matched_excerpt), ...] for one line of text."""
    hits: list[tuple[str, str]] = []
    for label, pattern in NAMED_PATTERNS:
        m = pattern.search(line)
        if m:
            hits.append((label, m.group(0)[:80]))
    if not _NOISE_LINE_RE.search(line):
        for m in _ENTROPY_TOKEN_RE.finditer(line):
            token = m.group(0)
            if _looks_like_hash(token):
                continue
            # A bare `identifier=IDENTIFIER`/`FLAG=value` code line (no digit anywhere) is almost
            # always a kwarg, constant or env-flag assignment, not a secret - real tokens/keys
            # overwhelmingly mix in digits (base64, hex, JWTs, vendor key formats all do).
            if not any(ch.isdigit() for ch in token):
                continue
            if shannon_entropy(token) >= ENTROPY_THRESHOLD:
                hits.append(("high_entropy_token", token[:80]))
    return hits


def _run_git(args: list[str]) -> str:
    result = subprocess.run(  # noqa: S603
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return result.stdout


def list_tree_files() -> list[str]:
    """Tracked + untracked-but-not-gitignored files (what would actually be published)."""
    out = _run_git(["ls-files", "--cached", "--others", "--exclude-standard"])
    return [line for line in out.splitlines() if line.strip()]


def scan_tree(policy: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    for rel_path in list_tree_files():
        if is_excluded(rel_path, policy):
            continue
        full = REPO_ROOT / rel_path
        try:
            text = full.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable; not in scope for a text-pattern scan
        for line_no, line in enumerate(text.splitlines(), start=1):
            for label, excerpt in _scan_line(line):
                reason = find_acceptance(rel_path, label, policy) or find_acceptance(
                    rel_path, excerpt, policy
                )
                findings.append(
                    Finding(
                        origin="tree",
                        path=rel_path,
                        label=label,
                        line_no=line_no,
                        excerpt=excerpt,
                        accepted=reason is not None,
                        accept_reason=reason or "",
                    )
                )
    return findings


_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$")
_DIFF_COMMIT_RE = re.compile(r"^commit ([0-9a-f]{7,40})")


def scan_history(policy: dict[str, Any]) -> list[Finding]:
    """Scan every added line (`+...`) across the full history of every branch/ref."""
    findings: list[Finding] = []
    diff_text = _run_git(["log", "--all", "-p", "--no-color", "--unified=0"])
    current_commit = ""
    current_path = ""
    for raw_line in diff_text.splitlines():
        commit_m = _DIFF_COMMIT_RE.match(raw_line)
        if commit_m:
            current_commit = commit_m.group(1)
            continue
        file_m = _DIFF_FILE_RE.match(raw_line)
        if file_m:
            current_path = file_m.group(1)
            continue
        if not raw_line.startswith("+") or raw_line.startswith("+++"):
            continue
        if not current_path or is_excluded(current_path, policy):
            continue
        content = raw_line[1:]
        for label, excerpt in _scan_line(content):
            reason = find_acceptance(current_path, label, policy) or find_acceptance(
                current_path, excerpt, policy
            )
            findings.append(
                Finding(
                    origin="history",
                    path=current_path,
                    label=label,
                    line_no=None,
                    excerpt=excerpt,
                    commit=current_commit,
                    accepted=reason is not None,
                    accept_reason=reason or "",
                )
            )
    return findings


def render_report(tree_findings: list[Finding], history_findings: list[Finding]) -> str:
    lines: list[str] = []
    lines.append("Secrets scan report (scripts/secrets_scan.py, ST-21-07)")
    lines.append("=" * 60)
    lines.append("")

    def render_group(title: str, findings: list[Finding]) -> None:
        lines.append(f"{title}: {len(findings)} finding(s)")
        for f in findings:
            tag = "ACCEPTED" if f.accepted else "OPEN"
            where = f"{f.path}:{f.line_no}" if f.line_no is not None else f.path
            commit = f" @{f.commit[:10]}" if f.commit else ""
            lines.append(f"  [{tag}] {f.label} in {where}{commit}: {f.excerpt!r}")
            if f.accepted:
                lines.append(f"           accepted: {f.accept_reason}")
        lines.append("")

    render_group("Working tree", tree_findings)
    render_group("Git history (all refs, added lines)", history_findings)

    all_findings = tree_findings + history_findings
    open_findings = [f for f in all_findings if not f.accepted]
    lines.append(f"Total: {len(all_findings)} finding(s), {len(open_findings)} open (not accepted)")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Exit 1 if any open finding remains.")
    parser.add_argument(
        "--tree-only",
        action="store_true",
        help="Skip the git-history scan (faster, for local use).",
    )
    args = parser.parse_args(argv)

    policy = load_policy()
    tree_findings = scan_tree(policy)
    history_findings = [] if args.tree_only else scan_history(policy)
    report = render_report(tree_findings, history_findings)
    print(report)

    open_findings = [f for f in tree_findings + history_findings if not f.accepted]
    if args.check and open_findings:
        print(f"secrets_scan --check: FAIL ({len(open_findings)} open finding(s))", file=sys.stderr)
        return 1
    if args.check:
        print("secrets_scan --check: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

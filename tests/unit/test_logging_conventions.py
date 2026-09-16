"""B-4: structlog calls in `src/` must never pass a reserved keyword as an event-dict key.

structlog reserves `event` (the message), `level`, `timestamp` and `logger` for its own
processors; a caller passing one of those as a keyword argument silently overwrites or corrupts
the log record. `exc_info` is a stdlib-logging-compatible kwarg structlog understands and is
allowed. This scans every `src/**/*.py` with `ast` (no import side effects) for calls shaped like
`log.<level>(...)` or `self._log.<level>(...)` and fails with file:line for every reserved kwarg.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"

LOG_LEVELS = {"debug", "info", "warning", "error", "critical", "exception"}
RESERVED_KWARGS = {"event", "level", "timestamp", "logger"}
ALLOWED_EXTRA = {"exc_info"}


def _is_log_call(func: ast.expr) -> bool:
    """Match `log.<level>(...)` and `self._log.<level>(...)` (or any `*.log.<level>(...)` chain)."""
    if not isinstance(func, ast.Attribute) or func.attr not in LOG_LEVELS:
        return False
    receiver = func.value
    if isinstance(receiver, ast.Name):
        return receiver.id == "log"
    if isinstance(receiver, ast.Attribute):
        return receiver.attr in ("log", "_log")
    return False


def _find_violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_log_call(node.func):
            continue
        for kw in node.keywords:
            if kw.arg in RESERVED_KWARGS and kw.arg not in ALLOWED_EXTRA:
                rel = path.relative_to(REPO_ROOT)
                violations.append(f"{rel}:{node.lineno}: reserved structlog kwarg {kw.arg!r}")
    return violations


def test_no_reserved_structlog_kwargs() -> None:
    violations: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        violations.extend(_find_violations(path))
    assert not violations, "reserved structlog kwargs found:\n" + "\n".join(violations)

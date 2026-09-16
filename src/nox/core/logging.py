"""Structured logging (structlog): JSON to a daily-rotating file plus console, with a PII/secret
filter.

Implements the engineering brief "never log secrets" and Failure and Recovery Model retention
("rotate daily, keep N days"). `pii_filter` runs before every renderer: values under secret-like
keys (token, secret, password, api_key, authorization, pin) are replaced, and e-mail addresses,
long hex and long base64-looking strings inside string values are masked. `get_logger(name)` is
the only entry point other modules use.
"""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import re
import sys
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import structlog

REDACTED = "[REDACTED]"
_SECRET_KEY = re.compile(
    r"(?:^|[_\-.\s])(?:token|secret|password|passwd|pwd|api_?key|authorization|auth|pin|credential)"
    r"(?:$|[_\-.\s])",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
# base64/url-safe tokens: 32+ chars, must contain a digit and a letter (avoids plain long
# words/paths).
_B64 = re.compile(
    r"(?<![A-Za-z0-9+/_-])(?=[A-Za-z0-9+/_-]*\d)(?=[A-Za-z0-9+/_-]*[A-Za-z])[A-Za-z0-9+/_-]{32,}={0,2}"
)

_MAX_DEPTH = 8


def is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY.search(key))


def mask_string(value: str) -> str:
    """Mask e-mail addresses, long hex strings and long base64-like strings."""
    value = _EMAIL.sub("[email]", value)
    value = _HEX.sub("[hex]", value)
    return _B64.sub("[b64]", value)


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        return "[...]"
    if isinstance(value, str):
        return mask_string(value)
    if isinstance(value, dict):
        return {
            str(k): (REDACTED if is_secret_key(str(k)) else _scrub(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [_scrub(v, depth + 1) for v in value]
    return value


def pii_filter(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: redact secret-like keys and mask PII in every string of the event."""
    for key in list(event_dict.keys()):
        if key in ("level", "logger", "timestamp", "exc_info", "stack_info"):
            continue
        if is_secret_key(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _scrub(event_dict[key])
    return event_dict


_handlers: list[logging.Handler] = []


def configure_logging(
    logs_dir: Path | None,
    *,
    level: str = "INFO",
    json_file: bool = True,
    pii: bool = True,
    retention_days: int = 14,
    console: bool = True,
    console_stream: Any = None,
    filename: str = "nox.log",
) -> list[logging.Handler]:
    """Configure structlog + stdlib logging. Returns the installed handlers (tests inspect/close
    them).

    `logs_dir=None` disables the file sink. Rotation happens at midnight; `retention_days` old
    files kept.
    """
    shutdown_logging()
    numeric_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if pii:
        shared.append(pii_filter)

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        cache_logger_on_first_use=False,
    )

    root = logging.getLogger()
    root.setLevel(numeric_level)
    handlers: list[logging.Handler] = []

    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            logs_dir / filename,
            when="midnight",
            backupCount=retention_days,
            encoding="utf-8",
            utc=True,
        )
        file_renderer: Any = (
            structlog.processors.JSONRenderer()
            if json_file
            else structlog.dev.ConsoleRenderer(colors=False)
        )
        file_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    file_renderer,
                ],
                foreign_pre_chain=shared,
            )
        )
        handlers.append(file_handler)

    if console:
        console_handler = logging.StreamHandler(console_stream or sys.stderr)
        console_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.dev.ConsoleRenderer(colors=False),
                ],
                foreign_pre_chain=shared,
            )
        )
        handlers.append(console_handler)

    for handler in handlers:
        handler.setLevel(numeric_level)
        root.addHandler(handler)
    _handlers.extend(handlers)
    return handlers


def shutdown_logging() -> None:
    """Flush and remove the handlers installed by `configure_logging` (releases file locks)."""
    root = logging.getLogger()
    for handler in _handlers:
        with contextlib.suppress(Exception):  # best effort during shutdown
            handler.flush()
            root.removeHandler(handler)
            handler.close()
    _handlers.clear()


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger for `name` (module path)."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger

"""Logger access for the shell: nox.core.logging when present, plain structlog otherwise."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_get_logger: Callable[[str], Any]
try:  # nox.core.logging is owned by the core agent and may not exist yet in this checkout
    from nox.core.logging import get_logger as _core_get_logger

    _get_logger = _core_get_logger
except ImportError:  # pragma: no cover - depends on sibling package availability
    import structlog

    _get_logger = structlog.get_logger


def get_logger(name: str) -> Any:
    """Return a structlog-style logger (``log.info(event, **kv)``)."""
    return _get_logger(name)

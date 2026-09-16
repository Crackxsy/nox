"""Logger resolution for the security package: nox.core.logging when present, structlog otherwise.

The core package owns `nox.core.logging`; it may not exist yet while packages are built in parallel,
so the security package resolves it at call time instead of importing it at module load.
Security modules never log secret values, PINs, transcripts or memory content at any level.
"""

from __future__ import annotations

import importlib
from typing import Any

import structlog


def get_logger(name: str) -> Any:
    """Return a structlog-style bound logger for `name`."""
    try:
        core_logging = importlib.import_module("nox.core.logging")
    except ImportError:
        return structlog.get_logger(name)
    factory = getattr(core_logging, "get_logger", None)
    if factory is None:
        return structlog.get_logger(name)
    return factory(name)

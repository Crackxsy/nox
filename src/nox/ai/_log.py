"""Logger shim for the AI package: uses nox.core.logging when it exists, else structlog directly.

ENGINEERING.md prescribes ``nox.core.logging.get_logger``; that module is owned by the core
agent and may not exist yet, so this shim resolves it lazily and never hard-depends on it.
"""

from __future__ import annotations

import importlib
from typing import Any

import structlog


def get_logger(name: str) -> Any:
    """Return a structlog-style logger (``.info(event, **kv)``) for ``name``."""
    try:
        core_logging = importlib.import_module("nox.core.logging")
    except ImportError:
        return structlog.get_logger(name)
    return core_logging.get_logger(name)

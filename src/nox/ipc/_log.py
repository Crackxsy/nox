"""Logger accessor for nox.ipc: nox.core.logging.get_logger when present, else structlog."""

from __future__ import annotations

import importlib
from typing import Any

import structlog


def get_logger(name: str) -> Any:
    """Return a structured logger. Falls back to plain structlog until nox.core.logging exists."""
    try:
        module = importlib.import_module("nox.core.logging")
    except ImportError:
        return structlog.get_logger(name)
    getter = getattr(module, "get_logger", None)
    if getter is None:
        return structlog.get_logger(name)
    return getter(name)

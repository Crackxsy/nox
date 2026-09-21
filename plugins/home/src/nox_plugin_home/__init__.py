"""Home Assistant plugin package. Entry point: `create(api)`."""

from __future__ import annotations

from .plugin import HomePlugin, create

__all__ = ["HomePlugin", "create"]

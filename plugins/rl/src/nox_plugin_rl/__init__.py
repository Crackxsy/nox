"""Rocket League Coach plugin package. Entry point: `create(api)`."""

from __future__ import annotations

from .plugin import RlPlugin, create

__all__ = ["RlPlugin", "create"]

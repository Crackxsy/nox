"""Rocket League Coach plugin package (ST-12-01..08, Spec v0.3). Entry point: `create(api)`."""

from __future__ import annotations

from .plugin import RlPlugin, create

__all__ = ["RlPlugin", "create"]

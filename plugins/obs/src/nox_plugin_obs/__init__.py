"""OBS Studio plugin package. Entry point: `create(api)`."""

from __future__ import annotations

from .plugin import ObsPlugin, create

__all__ = ["ObsPlugin", "create"]

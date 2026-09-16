"""OBS Studio plugin package (ST-11-02/03, Spec v0.2 Stream Bot). Entry point: `create(api)`."""

from __future__ import annotations

from .plugin import ObsPlugin, create

__all__ = ["ObsPlugin", "create"]

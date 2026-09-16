"""Creative Apps plugin package (Spec v0.7 Creative Apps, EPIC-16). See `plugin.py` for the
implementation and `manifest.yaml` for the declared tools/events/config."""

from __future__ import annotations

from .plugin import CreativePlugin, create

__all__ = ["CreativePlugin", "create"]

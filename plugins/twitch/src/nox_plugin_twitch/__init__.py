"""Twitch chat bot plugin package (ST-11-04, Spec v0.2 Stream Bot). Entry point: `create(api)`."""

from __future__ import annotations

from .plugin import TwitchPlugin, create

__all__ = ["TwitchPlugin", "create"]

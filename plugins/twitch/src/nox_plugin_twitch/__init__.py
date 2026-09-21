"""Twitch chat bot plugin package (Stream Bot). Entry point: `create(api)`."""

from __future__ import annotations

from .plugin import TwitchPlugin, create

__all__ = ["TwitchPlugin", "create"]
